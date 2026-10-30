# MontyCloud API load tests

Locust scenarios that replay real browser request waterfalls against the
MontyCloud API. Calls inside a batch fire in parallel; batches run in order.

| Scenario | Run | Flow code | What it covers |
|---|---|---|---|
| Main journey | `scenarios/api/journey/locustfile.py` | `mcperf/api/journey.py`, `mcperf/api/pages/`, `mcperf/api/chat.py` | Home Page → WAFR → optional Health Events → optional AI Chat (WebSocket) |
| Cost Dashboard | `scenarios/api/cost_dashboard/locustfile.py` | `mcperf/api/cost_dashboard.py` | 4 batches, Total Spend and Spend Trend once per date-range preset |
| Inventory | `scenarios/api/inventory/locustfile.py` | `mcperf/api/inventory.py` | `/org/inventory-summary` by account, region and resource type |
| Day2SDK | `scenarios/api/day2sdk/locustfile.py` | `mcperf/api/day2sdk.py` | Day2SDK public API GETs, grouped by API tag, key-pair auth |

Each scenario folder holds a one-call `locustfile.py` and its `config.yaml`.

## Setup

From the repo root (Python 3.10+):

```bash
pip install -e ".[locust]"
```

```bash
cp scenarios/api/users.csv.example scenarios/api/users.csv
```

Fill in `users.csv` (gitignored). Columns: `Name, Email, Password, New Password`.
Sign-in uses `Email` + `New Password`, falling back to `Password`. Day2SDK uses
key pairs from `scenarios/api/day2sdk/.env` instead (see its `.env.example`).

## Running

```bash
python -m locust -f scenarios/api/journey/locustfile.py --headless \
  --users 10 --spawn-rate 2 --csv reports/journey --html reports/journey.html
```

Options every scenario accepts:

| Option | Effect |
|---|---|
| `--env stg1` | Target environment (`dev1`, `stg1`, `int1`, `eu`, `dev-eu`, `prd01`; `dev`/`stg` also work). Overrides `api.base_url`. |
| `--mc-config path.yaml` | Use another config file (or set `MCPERF_CONFIG`). |
| `--report-name name` | Overrides `report.report_name`. |
| `--csv <prefix>` | Required for the custom HTML report, which is written next to the CSV. |

Drop `--headless` to use the web UI at http://localhost:8089.

### Run modes (`load.run_mode`)

- **`single_journey`** — each user runs `load.iterations` journeys and stops; the
  test exits by itself once every user is done. Use with `--users N`, no `--run-time`.
- **`timed`** — users loop until `--run-time`; `iterations` is ignored.

`load.user_count` is how many CSV rows are loaded; `--users` is concurrency.
With `user_count: 10` and `--users 20`, the 10 accounts are reused round-robin.

## Sweeps

User-count ramp, one run per count (any scenario; extra args go to Locust):

```bash
python -m mcperf.runners.users_sweep --locustfile scenarios/api/inventory/locustfile.py --max-users 10
```

Chat-query sweep: the journey once per queries file listed in a manifest
(`scenarios/api/journey/executions/*.yaml`). Each run gets its own generated
config under `reports/sweep_configs/`; the scenario's `config.yaml` is never touched,
so sweeps can run in parallel from one checkout.

```bash
python -m mcperf.runners.chat_sweep --manifest scenarios/api/journey/executions/wafr.yaml --dry-run
```

```bash
python -m mcperf.runners.chat_sweep --manifest scenarios/api/journey/executions/wafr.yaml
```

Summarise a finished chat sweep into one CSV row per execution:

```bash
python -m mcperf.runners.execution_summary --manifest scenarios/api/journey/executions/wafr.yaml --reports-dir reports
```

## Reports

| Output | Where |
|---|---|
| Locust HTML | `--html` path |
| Locust CSV | `<csv prefix>_stats.csv`, `_failures.csv`, ... |
| Custom HTML | `custom_<report_name>_<timestamp>.html` next to the CSV |
| Session continuations | `<csv prefix>_session_timeouts.json` (journey with chat, when any occurred) |

Regenerate the custom report by hand:

```bash
python -m mcperf.reporting.locust_report --csv reports/journey_stats.csv
```

Every request name starts with a section prefix — `[Auth]`, `[HomePage]`, `[WAFR]`,
`[Health]`, `[Chat]`, `[Cost]`, `[Inventory]`, `[Day2SDK]` — and the custom report
groups rows by it. URLs with IDs use template names (`.../workload/{id}/findings`)
so repeated calls aggregate.

## Journey details

**Modes.** `health:` and `chat:` each have `enabled` and `mode`:

- `appended` — Home → think → WAFR → [think → Health] → [think → Chat]
- `standalone` — sign in → think → that flow only. If both are standalone, chat wins.

**Health Events** mirrors the HAR waterfall: policies; summary + open breakdown in
parallel; closed and upcoming breakdowns; two event lists; then timelines and
affected resources for a random open event, in parallel. With no open events the
last step is skipped with a warning.

**AI Chat** opens one WebSocket per call to `chat.ws_base_url` with the user's
token and own `OrganizationId`, sends one random query from `chat.queries_file`
with **every** tenant in `chat.tenants_file` as `tenant_scope`, and reads frames
until `PROMPT_STATUS: ENDED` or `chat.timeout_seconds`.

- Metrics: `[Chat] time_to_first_token` (first REASONING frame) and
  `[Chat] full_response` (carries pass/fail).
- A top-level `PROMPT_STATUS` ERROR or REJECTED fails the call at once.
- "Endpoint request timed out" from API Gateway is a warning; reading continues.
- `SESSION_TIME_LIMIT_REACHED`: a new connection sends `"yes Continue"` on the
  same `thread_id`, up to `chat.max_session_retries` times per call. Counts per
  user appear in the report.
- `chat.transcript_log` records every frame; blank disables it.
- One query per connection; multi-turn on one connection is not implemented.

## Adding a scenario

1. `mcperf/api/<flow>.py`: `<Flow>Config(ScenarioConfig)` for its config section
   and `<Flow>User(MontyCloudUser)` implementing `run_flow()` with
   `self.call(...)` / `self.run_batch(...)`. Prefix request names `[<Flow>]`.
   Reuse `mcperf/api/pages/` if the flow includes an existing page.
2. `scenarios/api/<flow>/locustfile.py` (one `register_scenario(...)` call) and `config.yaml`.
3. Add the `[<Flow>]` section to `mcperf/reporting/locust_report.py` (row bucketing,
   section list, card, CSS) until the section registry lands.
4. A test under `tests/`; `tests/scenarios/` runs scenarios against a local mock API.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Users CSV not found` | Create `scenarios/api/users.csv`, or fix `load.users_csv` |
| `signin failed: HTTP 401` | Check `Email` / `New Password` in the CSV |
| `No DAY2SDK credentials found` | Add key pairs to `scenarios/api/day2sdk/.env` |
| `No WAFR workloads returned` | The user has no PENDING workloads in that environment |
| No custom HTML report | Pass `--csv <prefix>` |
| `chat.ws_base_url is not configured` | Set it, or disable chat |
| Chat times out | Raise `chat.timeout_seconds`; check the WebSocket URL and token |
