# KB Upload Load Test

Generates concurrent load against the Tenant KB document-upload flow
(`POST .../documents/upload_url` → `PUT` to the presigned S3 URL). Load
generation only — metrics/analysis are handled separately via log filters.

## Setup

```bash
pip install -r requirements.txt
export KB_LOADTEST_TOKEN=<jwt>        # pre-obtained auth token
```

The corpus lives in `./corpus`.

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

## Notes

- The token is pre-obtained; if it expires the preflight check fails fast —
  re-export a fresh `KB_LOADTEST_TOKEN` and rerun.
- The corpus is a fixed set of 32 real documents committed under `corpus/`
  (14 pdf, 12 docx, 4 png, 1 xlsx, 1 jpg — roughly 12 KB to 8.6 MB, 33 MB
  total), covering formats the KB service supports (`constants.DOCUMENT_FORMATS`
  + `IMAGE_FORMATS`). `--file-type` filters by extension; `random` picks from
  all of them. To vary the mix, add or remove files in `corpus/`.
