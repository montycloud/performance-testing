# Instructions.md — API Performance Test Framework

Read this first if you're picking up work in `API Performance test/`. It gives
a fast orientation to the codebase so you don't need to re-derive it from
scratch. For the specific design rationale behind the Chat/WebSocket flow
(decisions, trade-offs, deferred work), see
[WEBSOCKET_CHAT_APPROACH.md](WEBSOCKET_CHAT_APPROACH.md).

## What this is

A single-file Locust load test (`locustfile.py`) simulating a MontyCloud
user's browser journey against the API. One `HttpUser` subclass
(`MontyCloudUser`) drives everything; behavior is controlled entirely via
`config.yaml` — there is no CLI-flag-driven branching beyond Locust's own
`--users`/`--spawn-rate`/`--run-time`/`--csv`/`--html`.

```
python3 -m locust -f locustfile.py --headless --users N --spawn-rate R \
  --html reports/locust_report.html --csv reports/stats
```

## Module map

| File | Responsibility |
|---|---|
| `locustfile.py` | Everything: config/CSV bootstrap at module load, the `MontyCloudUser` class, all flow methods, and the `on_test_stop` hook that auto-generates the custom HTML report. |
| `config.yaml` | All tunables — API base URL, users CSV path, run mode/iterations, think times, and per-flow `health:`/`chat:` sections (`enabled`/`mode` toggles). |
| `report_generator.py` | Reads Locust's `--csv` stats output and renders a sectioned custom HTML report (`reports/custom_<name>_<ts>.html`), bucketing rows by their `[Prefix]` in the request `name`. |
| `users.csv` | Test user credentials (`Name,Email,Password,New Password,Tenant`). The `Tenant` column is no longer read by the Chat flow. |
| `chat_queries.txt` | Plain-text prompts for the Chat flow, one per line. |
| `Tenant.json` | User-provided list of `{ID, Name, ...}` tenant entries; **all** entries are sent in every chat call's `tenant_scope` (built once at module load, not per-user). |
| `executions.yaml` | Manifest for `run_chat_executions.py` — a list of `{name, queries_file, description}` runs sharing one `users`/`spawn_rate`/`run_time` block. |
| `run_chat_executions.py` | Standalone sweep runner — see "Chat-query sweep runner" section below. Does **not** modify `locustfile.py`/`report_generator.py`. |

## The five flows (all in `MontyCloudUser`)

Each flow is a `_xxx_flow()` method, all request names are prefixed
`[Auth]`/`[HomePage]`/`[WAFR]`/`[Health]`/`[Chat]` so both Locust's own stats
and `report_generator.py` can bucket them into sections.

1. **`_signin()`** — always runs once per user (`on_start`), `POST /auth/signin`
   then `GET /auth/user` to capture `self._token` and `self._org_id`.
2. **`_homepage_flow()`** — 5 sequential batches, calls within a batch fire in
   parallel via `gevent.pool.Group` (see `_run_batch`).
3. **`_wafr_flow()`** — 4 sequential batches; fetches a `WorkloadId` in batch 1
   and reuses it in the rest.
4. **`_health_flow()`** — optional (`health.enabled`), `health.mode` is
   `appended` (tacked on after Home/WAFR) or `standalone` (skips Home/WAFR).
5. **`_chat_flow()`** — optional (`chat.enabled`), WebSocket-based, same
   `appended`/`standalone` pattern via `chat.mode`. Uses `websocket-client`
   (not Locust's `HttpUser` client) and manually fires
   `environment.events.request.fire(...)` to get its two metrics
   (`[Chat] time_to_first_token`, `[Chat] full_response`) into Locust's stats.
   **Currently one query per call** — opens a connection, sends one query
   (with an empty `thread_id`, i.e. always a new conversation), waits for
   `PROMPT_STATUS: ENDED` (or `chat.timeout_seconds`), closes.
   Two terminal top-level `PROMPT_STATUS` frames fail the call immediately
   instead of waiting out the timeout: `message` as an `{"message": "ERROR"}`
   dict, and `message == "REJECTED"` (e.g. `code: SESSION_TIME_LIMIT_REACHED`)
   — the latter's `display_message`/`code` are surfaced in the failure reason.
   Multi-turn-on-one-connection is a known deferred item. The WS URL's
   `OrganizationId` param is `self._org_id` (signed-in user's own org, same
   as Home/WAFR/Health) but the message body's `tenant_scope` always lists
   *every* tenant from `Tenant.json` (`ALL_TENANT_SCOPE`, built once at
   module load) — not a per-user lookup; skipped only if `Tenant.json` yields
   zero entries.

`full_journey()` (the single `@task`) decides which flows run and in what
order, based on `_CHAT_ENABLED`/`_CHAT_MODE`/`_HEALTH_ENABLED`/`_HEALTH_MODE`.
If both `chat.mode` and `health.mode` are `standalone`, chat wins (logged
warning) — see the precedence check near the module-level config bootstrap.

## Run modes (`test.run_mode` in config.yaml)

- **`single_journey`** — each user runs `full_journey()` `test.iterations`
  times then raises `StopUser`. A watchdog (`on_test_start` listener) polls
  the runner and calls `runner.quit()`/`runner.stop()` once every user has
  finished, since Locust doesn't exit on its own when users self-stop.
- **`timed`** — users loop until `--run-time` expires; `iterations` ignored.

## Adding a new optional flow (pattern to follow)

This is exactly how the Chat flow was added — reuse this pattern for the next
one:

1. Add a new top-level section to `config.yaml` with at least `enabled` and
   `mode` (`appended`/`standalone`) keys, following `health:`/`chat:`.
2. In `locustfile.py`'s bootstrap section (~top third of the file, alongside
   `_HEALTH_ENABLED`/`_CHAT_ENABLED`), read the new config section into
   module-level constants.
3. Add a `_yourflow_flow()` method on `MontyCloudUser`, using `_get()` for
   HTTP calls (or a manual client + `events.request.fire(...)` for anything
   non-HTTP) and `[YourFlow]`-prefixed request names.
4. Wire it into `full_journey()`'s standalone/appended branches, in the same
   place Health/Chat are wired in.
5. Thread a `your_rows` list through `report_generator.py` everywhere
   `chat_rows`/`health_rows` currently appear: row-bucketing in `generate()`,
   `_throughput_section_html()`, and `_full_html()`'s section list — plus a
   `.dot-yourflow` / `.section-title.yourflow` CSS pair.
6. Document the new config keys and flow behavior in `README.md`.

## Chat-query sweep runner (`run_chat_executions.py`)

Added to run the chat test across multiple `chat_queries` files/descriptions
back-to-back without ever touching `locustfile.py`/`report_generator.py` and
without leaving a lasting diff in `config.yaml`. Design:

- `_CONFIG_FILE` in `locustfile.py` is hardcoded to `config.yaml` next to it
  (no env-var override) — deliberately left untouched per a "zero impact to
  existing code" requirement.
- Instead, `run_chat_executions.py` reads `executions.yaml`, and per entry:
  backs up `config.yaml`'s raw text, `yaml.safe_dump`s a modified copy over it
  (`chat.queries_file`, `test.description`, `test.report_name`,
  `chat.transcript_log` overridden; everything else inherited from the base
  config), shells out to `locust -f locustfile.py --headless ...` exactly like
  a manual run, then restores the original `config.yaml` bytes in a
  `try/finally` (+ SIGINT/SIGTERM handlers) once the whole sweep ends.
- A `config.yaml.sweep-backup` sentinel file guards against double-mutation if
  a previous sweep crashed before restoring; the script refuses to start if it
  finds one.
- Runs are necessarily sequential within one checkout (shared `config.yaml`);
  don't try to parallelize a sweep in the same working directory.
- `yaml.safe_dump` strips `config.yaml`'s inline comments while a run is in
  flight (comments come back once the original text is restored) — cosmetic,
  not a bug.

See the README's "Running multiple chat-query executions (sweep)" section for
usage.

## Known deferred work / open items

- **Chat multi-turn**: reusing one WebSocket connection across several
  queries (a real back-and-forth conversation) is not implemented. Current
  `_chat_flow()` is strictly one-query-per-connection. If implemented later,
  prefer a dedicated `chat.turns_per_connection` config key over overloading
  `test.iterations` (see WEBSOCKET_CHAT_APPROACH.md for why).
- Chat's `tenant_scope` sends **all** entries from `Tenant.json` (config:
  `chat.tenants_file`) on every call; `users.csv`'s `Tenant` column is no
  longer used for this. The call is skipped only if `Tenant.json` has zero
  usable entries.
- **Branch caveat (learned 2026-07-30)**: `locustfile.py`, `config.yaml`,
  `report_generator.py`, `requirements.txt`, and `README.md` are tracked by
  git — switching branches changes their content. `chat_queries.txt`,
  `Tenant.json`, `Instructions.md`, and `WEBSOCKET_CHAT_APPROACH.md` are
  currently **untracked**, so they persist across branch switches regardless
  of which branch's tracked files you're on. If chat code seems to have
  "disappeared", check `git status`/`git branch --show-current` before
  assuming something broke — it likely just needs re-implementing on the
  current branch (or the untracked files need to be committed).

## Verifying changes

There's no test suite. After changing `locustfile.py` or
`report_generator.py`, at minimum:

```bash
python3 -m py_compile locustfile.py report_generator.py
```

then a real headless run (small `--users`, `single_journey`,
`iterations: 1`) against a working staging environment, checking the
generated `reports/custom_*.html` renders the expected sections without
Python exceptions in the console output.
