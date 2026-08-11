# MontyCloud API Performance Test

Locust-based performance test that simulates the full user journey:

**Sign-in → Home Page (5 sequential batches) → WAFR Page (4 sequential batches) → (optional) Health Events**

Within each batch all HTTP calls fire in parallel; batches are strictly sequential.

The Health Events flow is optional (disabled by default) and can either be appended
to the end of the journey above, or run standalone as Sign-in → Health Events only.
See [Health Events flow](#health-events-flow) below.

---

## Prerequisites

- Python 3.9 +
- A MontyCloud staging environment reachable from your machine
- Users pre-created via `Scripts/CreateChildUsers/create_child_users.py`

---

## Setup

```bash
cd "API Performance test"

# 1. Install dependencies
pip install -r requirements.txt

# 2. (Optional) copy .env.example if you need env-var overrides
cp .env.example .env
```

---

## Configuration — `config.yaml`

| Key | Default | Description |
|-----|---------|-------------|
| `api.base_url` | `https://stg1-api.montycloud.com` | Target API environment |
| `api.mc_debug_mode` | `true` | Passed as `MC_DEBUG_MODE` in every signin payload |
| `api.timeout_seconds` | `40` | Per-request timeout |
| `test.users_csv` | `../Scripts/CreateChildUsers/users.csv` | Path to users CSV (relative to this directory or absolute) |
| `test.user_count` | `10` | **How many users to load from the CSV (first N rows)** |
| `test.spawn_rate` | `2` | Default spawn rate (users/second); override with `--spawn-rate` |
| `test.run_time` | `5m` | Default run time; override with `--run-time` |
| `test.iterations` | `1` | Journeys per user in `single_journey` mode; ignored in `timed` mode |
| `health.enabled` | `false` | Whether the Health Events flow runs at all |
| `health.mode` | `appended` | `appended` (Home Page → WAFR → think time → Health) or `standalone` (Signin → think time → Health only) |
| `chat.enabled` | `false` | Whether the Chat (WebSocket) flow runs at all |
| `chat.mode` | `appended` | `appended` (...WAFR/Health → think time → Chat) or `standalone` (Signin → think time → Chat only) |
| `chat.ws_base_url` | `""` | WebSocket endpoint, e.g. `wss://<id>.execute-api.<region>.amazonaws.com/<stage>` |
| `chat.queries_file` | `./chat_queries.txt` | Plain-text file, one chat prompt per line; a random line is picked per chat call |
| `chat.tenants_file` | `./Tenant.json` | JSON list of `{ID, Name, ...}` tenant entries; **all** entries are sent in every chat call's `tenant_scope` |
| `chat.model_id` / `temperature` / `top_p` / `top_k` | see config.yaml | Metadata sent with every chat query |
| `chat.timeout_seconds` | `120` | Max time to wait for the `PROMPT_STATUS: ENDED` frame before failing the call |
| `chat.max_session_retries` | `5` | **Max automatic retries when server returns `SESSION_TIME_LIMIT_REACHED`** — each query gets up to N continuation attempts before failing |
| `chat.transcript_log` | `./reports/chat_transcript.log` | Optional per-message transcript log; blank disables it |

### `user_count` vs `--users`

| `user_count` (config) | `--users` (Locust CLI) | Behaviour |
|----------------------|----------------------|-----------|
| 10 | 10 | 10 unique credentials, 10 concurrent |
| 10 | 20 | 10 credentials recycled round-robin, 20 concurrent |
| 100 | 10 | First 10 of 100 rows used, 10 concurrent |

---

## User CSV Format

The CSV must use the same column names as `Scripts/CreateChildUsers/users.csv`:

| Column | Used for |
|--------|----------|
| `Email` | Username in signin payload |
| `New Password` | Password in signin payload (permanent post-reset password) |
| `Password` | Fallback if `New Password` is blank |
| `Name` | Informational only |

---

## Running the Test

Set `run_mode` in `config.yaml` to choose how users behave, then pick the
matching command below.

---

### Option 1 — Timed run (`run_mode: "timed"`)

Users loop through the full journey continuously until `--run-time` expires.
Good for sustained load and throughput measurement over a fixed window.

```yaml
# config.yaml
test:
  run_mode: "timed"
```

```bash
locust -f locustfile.py --headless \
  --users 20 --spawn-rate 2 --run-time 5m \
  --html reports/locust_report.html \
  --csv reports/stats
```

---

### Option 2 — Single journey per user (`run_mode: "single_journey"`)

Each of the N users runs the full journey **`iterations` times** (default: once),
then stops itself. No new user is spawned in its place. Locust exits
automatically when the last user finishes — **no `--run-time` needed**.

```yaml
# config.yaml
test:
  run_mode: "single_journey"
  iterations: 1
```

```bash
locust -f locustfile.py --headless \
  --users 20 --spawn-rate 2 \
  --html reports/locust_report.html \
  --csv reports/stats
```

To run a fixed number of iterations with a single user (e.g. 1 user × 20
journeys back-to-back), set `iterations: 20` and run with `--users 1`:

```bash
locust -f locustfile.py --headless \
  --users 1 --spawn-rate 1 \
  --html reports/locust_report.html \
  --csv reports/stats
```

> Note: sign-in happens once per user (in `on_start`) and the token is reused
> for every iteration. For long multi-iteration runs, make sure the JWT
> lifetime covers the full run, or 401s will appear mid-test.

---

### Interactive Web UI

```bash
locust -f locustfile.py
# Open http://localhost:8089 in your browser
```

> **Note:** The custom HTML report is only auto-generated when `--csv` is
> passed on the command line.

### Quick smoke-test (single user, 30 seconds)

```bash
# config.yaml: run_mode: "timed"
locust -f locustfile.py --headless \
  --users 1 --spawn-rate 1 --run-time 30s \
  --html reports/smoke_test.html \
  --csv reports/smoke
```

---

## Running multiple chat-query executions (sweep)

To run the chat test back-to-back across several different `chat_queries` files
(e.g. different query "types") — same users/concurrency every time, only the
queries file and description changing — use `run_chat_executions.py` instead of
editing `config.yaml` by hand for each run.

It reads a manifest (default `executions.yaml`), and for each entry temporarily
overwrites `config.yaml` with that entry's `chat.queries_file` / `test.description`
/ `test.report_name`, runs `locust` headlessly, then restores the **original**
`config.yaml` once the whole sweep finishes (or is interrupted) — so nothing is
permanently changed in tracked files.

```bash
# Validate the manifest without running anything or touching config.yaml
python3 run_chat_executions.py --dry-run

# Run the sweep (uses ./executions.yaml by default)
python3 run_chat_executions.py

# Use a different manifest (e.g. per pipeline stage)
python3 run_chat_executions.py --manifest sweeps/my_sweep.yaml
```

`executions.yaml` schema:

```yaml
run:                       # shared across every execution — "users stay the same"
  users: 1
  spawn_rate: 1
  run_time: ""             # blank = rely on single_journey auto-stop

executions:
  - name: net_cost_2_months       # used to suffix every output file
    queries_file: "./chat_queries_net_cost.txt"
    description: |
      AI Conversation Chat — Tenant Net Cost Report query set
```

Each execution's `name` suffixes all of its output files so nothing gets
overwritten between runs:

| Output | Path |
|--------|------|
| Custom HTML report | `reports/custom_<name>_<timestamp>.html` |
| Locust HTML report | `reports/locust_report_<name>.html` |
| Locust CSV stats | `reports/stats_<name>_*.csv` |
| Chat transcript log | `reports/chat_transcript_<name>.log` |

Exits non-zero if any execution's locust run failed, so it can be used
directly as a single CI/pipeline step. Runs are sequential within one checkout
(they share the same `config.yaml`); parallel pipeline jobs get their own
workspace so this isn't a constraint across jobs.

---

## Reports

After the test completes, two reports are available:

| Report | Location | Description |
|--------|----------|-------------|
| Locust HTML | `reports/locust_report.html` | Built-in Locust report with charts, percentiles, and per-endpoint tables |
| Custom HTML | `reports/custom_report_<timestamp>.html` | Sectioned report: Auth / Home Page / WAFR Page / Health Events / Chat (Health and Chat sections are empty unless their `enabled: true`) |

### Generating the custom report manually

```bash
python report_generator.py --csv reports/stats_stats.csv
# or with explicit output path:
python report_generator.py --csv reports/stats_stats.csv --output reports/my_report.html
```

---

## Request naming & grouping

All requests are tagged with a section prefix in the Locust `name` field:

| Prefix | Section |
|--------|---------|
| `[Auth]` | Sign-in and initial `/auth/user` |
| `[HomePage]` | Home Page batches 1–5 |
| `[WAFR]` | WAFR Page batches 1–4 |
| `[Health]` | Health Events flow (only when `health.enabled: true`) |
| `[Chat]` | Chat / WebSocket flow (only when `chat.enabled: true`) — two pseudo-requests: `time_to_first_token` and `full_response` |

Parameterised URLs use stable names (e.g. `[WAFR] /war-assessment/workload/{id}/findings`)
so Locust correctly aggregates repeated calls with different IDs.

---

## Health Events flow

Disabled by default (`health.enabled: false`). When enabled, `health.mode` picks how it
fits into the journey:

- **`appended`** (default) — the existing journey runs as-is (Home Page → WAFR), then
  after a think time (`think_time_min`/`think_time_max`) the Health Events flow runs.
- **`standalone`** — Home Page and WAFR are skipped entirely for the run; each user
  does Signin → think time → Health Events only.

Call sequence (mirrors a captured browser HAR trace — mostly sequential, with two
parallel pairs):

1. `GET /health/api/v1/policies`
2. **parallel:** `GET /health/api/v1/events/summary` + `GET .../events/breakdown-summary` (StatusCode=open)
3. `GET .../events/breakdown-summary` (StatusCode=closed)
4. `GET .../events/breakdown-summary` (StatusCode=upcoming)
5. `GET .../events/events` (EventTypeCode=AWS_ABUSE_PHISHING_CONTENT_REPORTED, StatusCode=open)
6. `GET .../events/events` (StatusCode=open) — a random `Id` from the `HealthEvents` list
   in this response is used for the next step
7. **parallel:** `GET .../events/{id}/event-timelines` + `GET .../events/{id}/affected-resources`

If no `HealthEvents` are returned in step 6, steps 7 are skipped and a warning is logged
(the same pattern used when WAFR has no pending workloads).

---

## Chat / WebSocket flow

Disabled by default (`chat.enabled: false`). When enabled, `chat.mode` picks how it
fits into the journey:

- **`appended`** (default) — the existing journey runs as-is (Home Page → WAFR →
  optional Health), then after a think time the Chat flow runs.
- **`standalone`** — Home Page, WAFR, and Health are skipped entirely for the run;
  each user does Signin → think time → Chat only.

> If both `health.mode` and `chat.mode` are set to `standalone` at the same time,
> Chat takes precedence (a startup warning is logged) — this combination isn't
> expected in normal use.

Each chat call:

1. Opens a WebSocket connection to `chat.ws_base_url`, with the signed-in user's
   JWT (`Authorization`) and `OrganizationId` (the signed-in user's own org,
   fetched via `/auth/user` during sign-in — same as the Home Page flow) as
   query params, plus `agentic=true`.
2. Loads every tenant entry from `chat.tenants_file` (default `Tenant.json`) —
   this happens once at startup, not per-user.
3. Sends one query — picked at random from `chat.queries_file` — with an empty
   `thread_id` (a new conversation), `metadata`
   (`model_id`/`temperature`/`top_p`/`top_k` from config.yaml), and
   `tenant_scope` containing **all** tenants from `Tenant.json` (e.g.
   `[{id1: name1}, {id2: name2}, ...]`) — every user's chat call sends the
   same full set, regardless of `users.csv`.
4. Streams frames until a `PROMPT_STATUS: ENDED` frame arrives, or
   `chat.timeout_seconds` elapses.
5. Closes the connection.

> **Note the two different org ids in play:** the WebSocket URL's
> `OrganizationId` query param is always the signed-in user's own org
> (`self._org_id`, same value used by Home Page/WAFR/Health). The `tenant_scope`
> in the message body lists tenant-specific ids from `Tenant.json` instead —
> this is intentional, not a bug.

Two metrics are recorded into Locust's stats under the `[Chat]` prefix:

| Metric | Measures |
|--------|----------|
| `[Chat] time_to_first_token` | Time from sending the query to the first `REASONING` frame |
| `[Chat] full_response` | Time from sending the query to `PROMPT_STATUS: ENDED` (or to a timeout/error). **This one carries pass/fail** for the chat call. |

An "Endpoint request timed out" frame (an informational AWS API Gateway notice,
not part of the normal frame sequence) is logged as a **warning** and does not
by itself fail the call — the read-loop keeps listening for further frames.

### Handling Session Timeouts (SESSION_TIME_LIMIT_REACHED)

When the server responds with a `SESSION_TIME_LIMIT_REACHED` rejection:

```json
{
  "type": "PROMPT_STATUS",
  "message": "REJECTED",
  "code": "SESSION_TIME_LIMIT_REACHED",
  "display_message": "Marvin has reached the time limit for this session. Do you want to continue?",
  "thread_id": "..."
}
```

The test framework **automatically retries** the chat flow instead of failing:

1. **Detects** the `SESSION_TIME_LIMIT_REACHED` code in the rejection frame
2. **Closes** the current WebSocket connection
3. **Opens a new connection** and sends `{"query": "yes Continue", "thread_id": <from-rejection>, ...}` with the original tenant scope and metadata
4. **Repeats** up to `chat.max_session_retries` times (default: 5 attempts per query)
5. **Fails** the query if all retries are exhausted

Each chat query gets its own independent retry budget (not shared across calls). The HTML report includes:
- A **"Session Continuations"** summary card showing total retry count
- A **detailed per-user table** showing how many continuations each user triggered

Example config:
```yaml
chat:
  max_session_retries: 5    # Allow up to 5 continuation attempts per query
```

The session timeout stats are also written to a JSON sidecar file (`<csv-prefix>_session_timeouts.json`) for archival.

Set `chat.transcript_log` (default `./reports/chat_transcript.log`) to get a
full per-message transcript — every frame sent/received, with a timestamp and
elapsed time since the query was sent. Leave it blank to disable.

> **Note:** each chat call currently opens **one connection and sends one
> query**, then closes — multi-turn conversations reusing a single connection
> are not yet supported. `test.iterations` behaves exactly as it does for
> Home Page/WAFR/Health: it controls how many times the *whole journey*
> (including a fresh chat call) repeats in `single_journey` mode. See
> [WEBSOCKET_CHAT_APPROACH.md](WEBSOCKET_CHAT_APPROACH.md) for the full design
> rationale and what's deferred.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `FileNotFoundError: Users CSV not found` | `test.users_csv` path is wrong | Update path in `config.yaml` |
| `Signin failed: HTTP 401` | Wrong credentials in CSV | Verify `Email` + `New Password` columns |
| `No WAFR workloads returned` | User has no PENDING workloads | Create workloads for test users or use a different environment |
| Custom HTML report not generated | `--csv` flag not passed | Add `--csv reports/stats` to your Locust command |
| Connection errors | Wrong `base_url` | Check `api.base_url` in `config.yaml` |
| `chat.ws_base_url is not configured` | `chat.enabled: true` but `ws_base_url` blank | Set `chat.ws_base_url` in `config.yaml` |
| `No chat queries loaded` | `chat_queries.txt` missing or empty | Check `chat.queries_file` path and that the file has at least one non-comment line |
| `No tenants loaded from Tenant.json` | `Tenant.json` is missing, empty, or has no entries with an `ID` | Check `chat.tenants_file` path and that `Tenant.json` has at least one valid entry |
| Chat call times out (`No PROMPT_STATUS/ENDED frame within Ns`) | Backend took longer than `chat.timeout_seconds`, or connection dropped | Increase `chat.timeout_seconds`; check `ws_base_url`/token validity |

---

## File structure

```
API Performance test/
├── locustfile.py          Main Locust scenario
├── config.yaml            Test configuration
├── report_generator.py    Custom HTML report builder
├── chat_queries.txt       Chat prompts (one per line) used by the Chat flow
├── Tenant.json            Tenant entries; all are sent in every chat call's tenant_scope
├── requirements.txt       Python dependencies
├── README.md              This file
├── WEBSOCKET_CHAT_APPROACH.md   Design notes for the Chat/WebSocket flow
├── .env.example           Environment variable template
└── reports/               Generated reports (git-ignored)
    ├── locust_report.html
    ├── custom_report_<ts>.html
    └── stats_stats.csv    (Locust raw CSV — used by custom report)
```
