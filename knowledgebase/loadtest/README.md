# KB Upload Load Test

Generates concurrent load against the Tenant KB document-upload flow
(`POST .../documents/upload_url` → `PUT` to the presigned S3 URL). Metrics are
fetched afterwards from CloudWatch Logs Insights (see "CloudWatch metrics" below).

## Setup

```bash
pip install -r requirements.txt
export KB_ROOT_PASSWORD=<msp root password>   # signs in via collections_config.yaml
```

`main.py` signs in itself using the root email/env in `collections_config.yaml`
(same file as Stage 1). Pass `--token`/`KB_LOADTEST_TOKEN` instead to skip
sign-in and use a pre-obtained JWT.

The corpus lives in `./corpus`.

## Collection setup (Stage 1)

Create the collection(s) to upload into, without a manual curl/browser step:

```bash
export KB_ROOT_PASSWORD=<msp root password>
python setup_collections.py --config collections_config.yaml
```

Edit `collections_config.yaml` first (env, root email, backend-required cookies,
and the list of collections to create). The script signs in as the MSP root
user, creates each collection, and writes `collections_output_<timestamp>.yaml`
with each collection's `id` — pass those ids as `--collection-id` to `main.py`
(comma-separated for multiple collections); `main.py` reuses the same
`collections_config.yaml` to sign in.

## Run

```bash
python main.py \
  --env dev1 \
  --collection-id <collection_id[,collection_id2,...]> \
  --concurrency 10 --batches 5 --batch-delay 2 \
  --file-type random          # or a specific ext: pdf, docx, xlsx, png, ...
```

- Load shape: `--batches` waves, each firing `--concurrency` uploads at once,
  sleeping `--batch-delay` seconds between waves. Total = concurrency × batches.
- Target env: `--env` (dev1, dev-eu, eu, int1, stg1, prd01) — the base URL is
  derived automatically. Use `--base-url` only to override it.
- Tenant: uploads use the token user's own org by default. Pass `--org-id
  <tenant>` only to target a specific tenant (sent as the `d2oid` cookie).
- Each request logs one JSON line to `logs/kb_loadtest_<run_id>.jsonl`
  (timestamp, worker id, org/collection id, document_id, file type/size, HTTP
  statuses, latencies, error). A success/failure tally prints at the end.

## Test data & cleanup

Every uploaded document is tagged with a per-run label `loadtest-<run_id>` and a
`loadtest-<run_id>-...` name, so it is easy to locate and purge. Note the
`run_id` printed at the start of a run, then:

```bash
python main.py --env dev1 --collection-id <cid> \
  --cleanup --run-id <run_id>
```

Deletion is asynchronous (docs go to `deleting`, then are purged downstream).

## CloudWatch metrics (Stage 3)

After a load-test run, fetch document-lifecycle timings straight from
CloudWatch Logs Insights — no manual console export needed:

```bash
python fetch_metrics.py --env stg1 --minutes-back 60 --format markdown
```

- `--env` picks which env's log groups to query (dev1, stg1, prd01, ...).
- `--minutes-back` sets the query window (from now minus N minutes to now).
- `--aws-profile` / `--aws-region` (default `us-east-2`) control the boto3
  session; credentials otherwise come from the default AWS credential chain.
- The raw Logs Insights export is always saved to
  `logs/cloudwatch_<env>_<timestamp>.json` for later re-analysis, in addition
  to printing the rendered table.
- Prints the same table as `analyze_kb_timings.py`
  (`s3_upload_time_s`, `metadata_creation_time_s`, `summarization_time_s`,
  `kb_ingestion_time_s` per document). You can also run
  `analyze_kb_timings.py` directly against any saved export file.

### Poll until a specific set of documents finish ingesting

Instead of a fixed time window, track specific document ids until they all
reach `kb_ingestion_completed` (or a timeout):

```bash
python fetch_metrics.py --env stg1 --document-ids <doc_id1,doc_id2> \
  --poll-every 30 --timeout 600
```

- Re-runs the CloudWatch query every `--poll-every` seconds, printing progress
  (e.g. "2/3 completed — waiting on: <id>") each cycle.
- Waits for **all** given ids to complete, or reports the ones still pending
  once `--timeout` (default 600s / 10 min) elapses.
- Saves the raw export once at the end, then renders the final table filtered
  to just those document ids.

## Full pipeline (Stage 1 → 2 → 3 in one command)

`run_pipeline.py` chains all three stages for CI/pipeline use — creates
collections, uploads documents, then polls CloudWatch until every uploaded
document finishes ingesting:

```bash
export KB_ROOT_PASSWORD=<msp root password>
python run_pipeline.py --config collections_config.yaml \
  --concurrency 10 --batches 5 --file-type random \
  --poll-every 30 --timeout 600
```

Each stage's standalone script (`setup_collections.py`, `main.py`,
`fetch_metrics.py`) still works exactly as documented above — use them
individually when you don't need the full chain (e.g. to re-run just Stage 3
against an existing upload).

## Notes

- The token is pre-obtained; if it expires the preflight check fails fast —
  re-export a fresh `KB_LOADTEST_TOKEN` and rerun.
- The corpus is a fixed set of 32 real documents committed under `corpus/`
  (14 pdf, 12 docx, 4 png, 1 xlsx, 1 jpg — roughly 12 KB to 8.6 MB, 33 MB
  total), covering formats the KB service supports (`constants.DOCUMENT_FORMATS`
  + `IMAGE_FORMATS`). `--file-type` filters by extension; `random` picks from
  all of them. To vary the mix, add or remove files in `corpus/`.
