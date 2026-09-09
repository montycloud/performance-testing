# Security Bot Rescan Performance Test

Simulates N concurrent users triggering Security Bot finding rescans, one user per
tenant (org), and records every API step's request/response for verification.
See [Security_bot.prd](Security_bot.prd) for the original requirement.

## Quick start (do these first)

1. **Create/activate a virtualenv and install deps** (a shared repo venv already exists at
   `../.venv` — reuse it instead of creating a new one if possible):
   ```bash
   cd Security_bot
   python3 -m venv ../.venv        # skip if ../.venv already exists
   ../.venv/bin/pip install -r requirements.txt
   ```

2. **Fill in `users.csv`** (copy from `users.csv.example` if it doesn't exist yet).
   Each row is one simulated user and MUST map to a unique org:
   ```csv
   Name,Email,Password,OrganizationId
   Perf Test User 1,perftestuser1@montycloud.com,<password>,42d37e6e-2a53-4093-95b7-86c1aa5e3c32
   ```
   - `OrganizationId` must be an `ID` from [organizations.json](organizations.json).
   - Orgs are assigned **strictly one-to-one** — two rows cannot share an org, and you
     cannot request more `--users` than you have rows/orgs.
   - `OrganizationId`, `Organization` (name), or `Tenant` (short label) columns are all
     accepted; `OrganizationId` is recommended since it's unambiguous.
   - `users.csv` is gitignored — never commit real passwords.

3. **Set up `.env`** (copy from `.env.example`):
   ```bash
   cp .env.example .env
   ```
   Only needed if you want a single-user fallback run without `users.csv`
   (`ROOT_EMAIL` / `ROOT_PASSWORD`). If `users.csv` exists, `.env` login vars are unused.

4. **Run it**:
   ```bash
   ../.venv/bin/python security_bot.py --users 7 -v
   ```
   Always try `--dry-run` first when changing config — it logs every planned call
   without sending anything:
   ```bash
   ../.venv/bin/python security_bot.py --dry-run --users 7 -v
   ```

   Other flags: `--findings-limit N` (override `run.findings_limit`), `--config path.yaml`.

## What it does (steps, in order)

1. `POST /auth/signin` — per-user login, captures `Token`/`AccessToken`/`RefreshToken`.
2. Select the org assigned to that user in `users.csv`.
3. `POST /auth/switch-context/organization/{org_id}` — switch into the tenant.
4. `POST /auth/refresh-token` — exchange for a **tenant-scoped** token, used for steps 5–6.
5. `GET /bots/{bot_id}/insights` — fetch up to `findings_limit` findings, server-side
   filtered to `findings_statuses` (default `["FAILED"]`), capped to `rescan_count` (default 10).
6. `POST /bots/api/{bot_id}/rescan` — **one request per finding** (see Limitations), fired
   in parallel for one user, and synchronized so all users start step 6 at the same time.

All users run concurrently (one Python thread each). Every step's request/response is
written to `logs/<run_ts>/user<NN>_step<K>_<name>.json`, plus `summary.json` and
`rescan_ids.txt` for the whole run. Sensitive values (`Token`, `Password`, etc.) are masked.

## Known limitations / server constraints

- **`FindingIds` must contain exactly one id per rescan request** — the API rejects
  batched ids (`"FindingIds must contain exactly one id"`). This is why step 6 fires
  one POST per finding instead of a single batched call.
- **Per-tenant concurrent rescan cap** — if a tenant already has too many rescans running,
  new ones fail with `"This tenant already has the maximum number of finding-level
  rescans running"`. This is expected under load and shows up as per-finding errors in
  `summary.json`, not a script bug.
- **Orgs are strictly 1:1 with users** — `--users N` requires N distinct rows in
  `users.csv`, each with a distinct `OrganizationId`. Running with more users than
  available orgs/rows fails fast with a clear error.
- **`db_check.py` (step 7 / Aurora MySQL query)** is a separate, disabled-by-default,
  post-run step — not yet wired into the main flow. Rescans are async, so DB checks
  should run well after `security_bot.py` completes, not inline.
- Locust-based load testing is planned but not yet implemented; today's driver is a
  Python `ThreadPoolExecutor`-based CLI only.

## TODO (details to fill in later)
- config.yaml field reference
- log file schema reference
- db_check.py usage once implemented
- Locust integration notes
