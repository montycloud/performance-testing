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
| `test.users_csv` | `./users.csv` | Path to users CSV (relative to this directory or absolute) |
| `test.user_count` | `50` | **How many users to load from the CSV (first N rows)** |
| `test.spawn_rate` | `50` | Informational only (shown in report); set actual value via `--spawn-rate` |
| `test.run_time` | `5m` | Informational only (shown in report); set actual value via `--run-time` |
| `test.iterations` | `20` | Journeys per user in `single_journey` mode; ignored in `timed` mode |
| `health.enabled` | `false` | Whether the Health Events flow runs at all |
| `health.mode` | `appended` | `appended` (Home Page → WAFR → think time → Health) or `standalone` (Signin → think time → Health only) |

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

## Reports

After the test completes, two reports are available:

| Report | Location | Description |
|--------|----------|-------------|
| Locust HTML | `reports/locust_report.html` | Built-in Locust report with charts, percentiles, and per-endpoint tables |
| Custom HTML | `reports/custom_report_<timestamp>.html` | Sectioned report: Auth / Home Page / WAFR Page / Health Events (Health section is empty unless `health.enabled: true`) |

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

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `FileNotFoundError: Users CSV not found` | `test.users_csv` path is wrong | Update path in `config.yaml` |
| `Signin failed: HTTP 401` | Wrong credentials in CSV | Verify `Email` + `New Password` columns |
| `No WAFR workloads returned` | User has no PENDING workloads | Create workloads for test users or use a different environment |
| Custom HTML report not generated | `--csv` flag not passed | Add `--csv reports/stats` to your Locust command |
| Connection errors | Wrong `base_url` | Check `api.base_url` in `config.yaml` |

---

## File structure

```
API Performance test/
├── locustfile.py          Main Locust scenario
├── config.yaml            Test configuration
├── report_generator.py    Custom HTML report builder
├── requirements.txt       Python dependencies
├── README.md              This file
├── .env.example           Environment variable template
└── reports/               Generated reports (git-ignored)
    ├── locust_report.html
    ├── custom_report_<ts>.html
    └── stats_stats.csv    (Locust raw CSV — used by custom report)
```
