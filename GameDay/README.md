# GameDay User Creation Tool

A Python CLI that bulk-provisions MontyCloud root users and their child users from a CSV file, with **up to 10 parallel workers** (configurable). Each root user gets a full setup: signup → email verification → signin → preferences → subscription → child user creation → tenant creation.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Prerequisites](#2-prerequisites)
3. [Setup](#3-setup)
4. [CSV Format](#4-csv-format)
5. [Commands Reference](#5-commands-reference)
6. [Step-by-Step Workflow](#6-step-by-step-workflow)
7. [⚠️ Manual Step — Email Verification](#7-manual-step--email-verification)
8. [Output Files](#8-output-files)
9. [Parallelism & Batching](#9-parallelism--batching)
10. [Configuration Reference](#10-configuration-reference)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Overview

| What | Detail |
|------|--------|
| **Script** | `gameday.py` |
| **Input** | CSV file with one row per root user |
| **API target** | `https://dev-api.montycloud.com` (dev environment only) |
| **Parallelism** | Configurable — default 10 workers |
| **Output** | Timestamped CSV copy + JSON results file |
| **Idempotent** | Re-running skips rows already marked `Status=SUCCESS` |

### What gets created per CSV row

```
Root User (signup + preferences + subscription)
└── Child User (created under root's org)
    ├── Child Tenant 1 (e.g. Unicorn.Rentals)
    └── Child Tenant 2 (e.g. Blume Corporation)
```

---

## 2. Prerequisites

- Python **3.9** or later
- Network access to `https://dev-api.montycloud.com`
- Ability to manually verify email addresses in the MontyCloud console (see [Section 7](#7-manual-step--email-verification))

---

## 3. Setup

```bash
# 1. Navigate to the GameDay directory
cd GameDay/

# 2. Activate virtual environment (if one exists at project root)
source ../.venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create your .env file from the example
cp .env.example .env

# 5. Edit .env — set at minimum:
#    BASE_URL, PARALLEL_WORKERS, CAPTCHA_CODE, MC_DEBUG_MODE

# 6. Prepare your CSV (see Section 4)
#    The file sample_users.csv is a starter template
```

---

## 4. CSV Format

### Input CSV

The input CSV must have **exactly these column headers** (case-sensitive):

| Column | Description | Example |
|--------|-------------|---------|
| `Root Email` | Email for the root / MSP account | `gameday+root01@example.com` |
| `Root Name` | Full name of the root user | `GameDay User 01` |
| `MSP Org Name` | Company / MSP organisation name | `NimbusMSP` |
| `Root Password` | Password for the root account | `Gamedayaws156!` |
| `Child Email` | Email for the child user under this root | `gameday+child01@example.com` |
| `Child Name` | Full name of the child user | `Child User 01` |
| `Child Password` | Password for the child account | `Gamedayaws156child!` |
| `Child Tenant 1 Name` | Name of the first tenant to create | `Unicorn.Rentals` |
| `Child Tenant 2 Name` | Name of the second tenant to create | `Blume Corporation` |

> **Password policy**: MontyCloud requires at least 8 characters with uppercase, lowercase, digit, and special character (e.g. `Gamedayaws156!`).

**Example CSV:**

```csv
Root Email,Root Name,MSP Org Name,Root Password,Child Email,Child Name,Child Password,Child Tenant 1 Name,Child Tenant 2 Name
gameday+root01@example.com,GameDay User 01,NimbusMSP,Gamedayaws156!,gameday+child01@example.com,Child User 01,Gamedayaws156child!,Unicorn.Rentals,Blume Corporation
gameday+root02@example.com,GameDay User 02,NimbusMSP,Gamedayaws156!,gameday+child02@example.com,Child User 02,Gamedayaws156child!,Unicorn.Rentals,Blume Corporation
```

### Output CSV (produced by `create-users`)

All original columns are preserved and the following are appended:

| Column | Description |
|--------|-------------|
| `Root Org ID` | Org ID from the root user's roles (used for child user creation) |
| `Child User ID` | `UserId` returned when creating the child user |
| `Status` | `SUCCESS` or `FAILED` |
| `Error` | Error message if `Status=FAILED`, empty otherwise |
| `Features Enabled` | `true` if the Lambda feature-enablement step succeeded, `false` if it failed, blank if the row did not reach Step 10 |
| `Tenant 1 ID` | `OrgId` of the first tenant created (inline after step 9) |
| `Tenant 2 ID` | `OrgId` of the second tenant created |
| `Tenant Status` | `SUCCESS` or `FAILED` (independent of `Status` — a user can be `SUCCESS` even if tenants fail) |
| `Tenant Error` | Error message if `Tenant Status=FAILED` |
| `API Key ID` | `APIKeyId` returned by the API key creation endpoint |
| `API Secret Key` | `APISecretKey` returned by the endpoint — treat as a secret |
| `Access Key` | `AccessKey` returned by the endpoint |

---

## 5. Commands Reference

### `create-users` — Provision root + child users

```
python gameday.py create-users [OPTIONS]

Options:
  --csv FILE      Path to input CSV file
                  (default: $CSV_FILE env var, or sample_users.csv)
  --workers N     Number of parallel workers
                  (default: $PARALLEL_WORKERS env var, or 10)
  --dry-run       Log API calls without executing; skip email verification gate
```

**Examples:**

```bash
# Run with defaults (sample_users.csv, 10 workers)
python gameday.py create-users

# Custom CSV and workers
python gameday.py create-users --csv my_users.csv --workers 5

# Dry-run to validate CSV and see what API calls would be made
python gameday.py create-users --csv my_users.csv --dry-run

# Re-run after partial failures — skips already-succeeded rows
python gameday.py create-users --csv users_output_20260701_120000.csv
```

---

### `create-tenants` — Create org tenants for provisioned users

```
python gameday.py create-tenants --csv OUTPUT_CSV [OPTIONS]

Options:
  --csv FILE      Path to the output CSV from create-users (required)
                  Must contain Status=SUCCESS rows with JWT token and Child User ID
  --workers N     Number of parallel workers (default: 10)
  --dry-run       Log API calls without executing
```

**Examples:**

```bash
# Create tenants for all successfully provisioned users
python gameday.py create-tenants --csv users_output_20260701_120000.csv

# Dry-run
python gameday.py create-tenants --csv users_output_20260701_120000.csv --dry-run
```

> **Note**: Tenants are created **automatically** as part of `create-users` (inline after step 9, using the root JWT from sign-in — no re-authentication). Use `create-tenants` only as a **fallback** if tenant creation failed for some rows — it reads the output CSV and retries rows where `Tenant Status=FAILED`.

---

### `--help`

```bash
python gameday.py --help
python gameday.py create-users --help
python gameday.py create-tenants --help
```

---

## 6. Step-by-Step Workflow

The `create-users` command executes the following steps for each row. Steps within a batch run in parallel across workers; steps within a single row run sequentially.

### Root User Provisioning

| Step | API Endpoint | Description |
|------|-------------|-------------|
| 1 | `POST /auth/signup` | Register the root user account. Sends `Name`, `CompanyName`, `Email`, `Password`, `Designation`, `CaptchaCode`, `MC_DEBUG_MODE`. |
| 2 | *(manual)* | Email verification — see [Section 7](#7-manual-step--email-verification). |
| 3 | `POST /auth/signin` | Sign in with root credentials. Saves `Token` (JWT), `Org ID` from the `Roles` array, and `RefreshToken`. |
| 3b | `POST /auth/refresh-token` | Exchange `RefreshToken` for a fresh `AccessToken` (required by change-password). |
| 4 | `POST /auth/change-password` | Update the root user's password. Sends `PreviousPassword`, `ProposedPassword`, `AccessToken`, `UserEmail`. |
| 4b | `POST /org/customer-preference` | Set `AutomatedCloudOps` as the feature preference with `CUR` and `COH` optional features enabled. |
| 5 | `POST /subscription` | Activate `Trial` subscription. |
| 6 | `POST /customersupport/submitrequest/` | Submit a customer support request with an empty payload `{}`. |

### Child User Provisioning

| Step | API Endpoint | Description |
|------|-------------|-------------|
| 7 | `POST /auth/user` | Create child user under root's org. Saves `UserId`. Role, Dept, and Description are hardcoded. |
| 8 | `POST /auth/signin` | Sign in as child user. Expects `ChallengeName: NEW_PASSWORD_REQUIRED` response. Saves `Session`. |
| 9 | `POST /auth/reset_temp_password` | Reset the temporary child user password using the `Session` from step 8. |
| 10 | AWS Lambda | Invoke `publish-platform-events` Lambda to fire a `customerUpdateRequested` event that enables `MTEnabled`, `FBPEnabled`, `AgenticMarvin`, and `AIApps` features for the root user. Result written to `Features Enabled` column. |

### Tenant Creation (inline, runs automatically after step 9)

| Step | API Endpoint | Description |
|------|-------------|-------------|
| T1 | `POST /org/organization/` | Create Tenant 1 under root's org. `Owner` is set to the root user's email. Reuses the JWT token from step 3 — no re-authentication needed. |
| T2 | `POST /org/organization/` | Create Tenant 2 under root's org. `Owner` is set to the root user's email. |
| T3 | `POST /day2/platform/api/v1/api-keys/` | Create an API key for the root user. Payload: `{"Name": ..., "ExpiryDays": ...}` (configurable). Saves `APIKeyId`, `APISecretKey`, and `AccessKey` to output CSV. |

---

## 7. ⚠️ Manual Step — Email Verification

> **This step cannot be automated.** After signing up each batch of users, the script **pauses and waits** for you to verify their email addresses before continuing.

### What happens

1. The script signs up all users in the current batch (up to `--workers` users at once).
2. The terminal displays a list of email addresses that need verification:
   ```
   ============================================================
     MANUAL STEP REQUIRED — Batch 1
   ============================================================
     3 user(s) just signed up:
       • gameday+root01@example.com
       • gameday+root02@example.com
       • gameday+root03@example.com

     Please verify their email addresses in the MontyCloud
     console or via the verification emails, then press Enter
     to continue with the rest of the provisioning steps.

     Press Enter when email verification is complete >
   ```
3. Go to the MontyCloud console and verify each email.
4. Press **Enter** in the terminal to continue with signin and remaining steps.

### Tips

- Keep the batch size (`--workers`) manageable — verifying 10 emails at once is faster than 30.
- Verification emails sometimes land in spam. Check junk folders.
- If an email fails verification before you press Enter, that row will fail at the signin step and be marked `Status=FAILED`. Re-run the script after fixing — succeeded rows are automatically skipped.

---

## 8. Output Files

Two output files are created in the `GameDay/` directory after each run, with a timestamp in the filename to prevent overwrites:

| File | Example Name | Description |
|------|-------------|-------------|
| Output CSV | `users_output_20260701_120000.csv` | Copy of input CSV with result columns appended (includes tenant IDs and status) |
| Results JSON | `results_output_20260701_120000.json` | All rows as a JSON array (useful for scripting / debugging) |

If `create-tenants` fallback is used, it produces:

| File | Example Name |
|------|-------------|
| Output CSV | `users_tenants_20260701_130000.csv` |
| Results JSON | `results_tenants_20260701_130000.json` |

### Using output as input for re-runs

The output CSV from `create-users` can be fed directly back as the `--csv` input for a re-run. Rows with `Status=SUCCESS` are automatically skipped — only failed or incomplete rows are retried.

```bash
# First run
python gameday.py create-users --csv my_users.csv

# Re-run after fixing issues — skips already-succeeded rows
python gameday.py create-users --csv users_output_20260701_120000.csv
```

---

## 9. Parallelism & Batching

```
Total rows in CSV
│
├── Batch 1  (rows 1–10, workers=10)
│   ├── [Parallel] Step 1: Signup all 10 users
│   ├── [PAUSE]   Manual email verification
│   └── [Parallel] Steps 3-9: Provision all 10 users
│
├── Batch 2  (rows 11–20)
│   ├── [Parallel] Step 1: Signup 10 users
│   ├── [PAUSE]   Manual email verification
│   └── [Parallel] Steps 3-9: Provision 10 users
│
└── ... (continues until all rows processed)
```

**Key points:**

- Workers are controlled by `--workers` (CLI) or `PARALLEL_WORKERS` (`.env`), default `10`.
- A new batch starts only after the previous batch fully completes (including verification gate).
- Within a batch, signup and post-verification steps both run in parallel using `ThreadPoolExecutor`.
- If one row fails, it does **not** affect other rows in the batch — each row's exception is caught independently.
- All I/O (CSV/JSON writes) uses a thread lock to prevent race conditions.

---

## 10. Configuration Reference

All non-secret configuration is in `config.yaml` (same directory as `gameday.py`). The only secret is in `.env`.

### `config.yaml` — `api` section

| Key | Default | Description |
|-----|---------|-------------|
| `base_url` | `https://dev-api.montycloud.com` | MontyCloud API base URL (no trailing slash) |
| `captcha_code_env_var` | `CAPTCHA_CODE` | Name of the `.env` variable holding the captcha/debug token |
| `mc_debug_mode` | `true` | Sends `MC_DEBUG_MODE: true` in API payloads |
| `timeout_seconds` | `30` | HTTP request timeout |

### `config.yaml` — `users` section

| Key | Default | Description |
|-----|---------|-------------|
| `parallel_workers` | `10` | Max concurrent workers. Can be overridden per-run with `--workers`. |
| `root_designation` | `Business Analyst` | Designation field in signup payload |
| `csv_file` | `sample_users.csv` | Default CSV path when `--csv` is not provided |

### `config.yaml` — `lambda` section (Step 10)

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Set to `false` to skip Lambda invocation entirely |
| `function_name` | `publish-platform-events-dev1-publish_platform_events` | Full Lambda function name to invoke |
| `region` | `us-east-1` | AWS region where the Lambda is deployed |
| `profile` | `dev1` | AWS named profile to use. **Leave blank** to use the default credential chain (env vars / `~/.aws/credentials`). |
| `enabled_features` | `MTEnabled/FBPEnabled/AgenticMarvin/AIApps: true` | Feature flags sent in the Lambda `EventData.EnabledFeature` payload |

### `config.yaml` — `api_key` section (Step T3)

| Key | Default | Description |
|-----|---------|-------------|
| `name` | `APIKey` | `Name` field in the API key creation payload |
| `expiry_days` | `90` | `ExpiryDays` field in the payload |

### `.env` — secrets only

| Variable | Description |
|----------|-------------|
| `CAPTCHA_CODE` | Captcha bypass token for dev/debug environments |

**Hardcoded values** (change in `gameday.py` if needed):

| Constant | Value | Description |
|----------|-------|-------------|
| `CHILD_ROLE_ID` | `d4c81030-68e2-11ee-8c99-0242ac120002` | Role assigned to all child users |
| `TENANT_FEATURE` | `AutomatedCloudOps` | Feature set for tenant creation |
| `TENANT_OPTIONAL_FEATURES` | `{"CUR": true, "COH": true}` | Optional features for each tenant |

---

## 11. Troubleshooting

### `Status=FAILED` at signup step

- **Cause**: Email address already registered or invalid format.
- **Fix**: Update the email in the CSV and re-run. Already-succeeded rows are skipped.

### `Status=FAILED` at signin step — "signin returned no Token"

- **Cause**: Email verification was not completed before pressing Enter.
- **Fix**: Verify the email in the MontyCloud console, then re-run. The script will retry only failed rows.

### `Status=FAILED` at signin step — `ChallengeName` not present (child user)

- **Cause**: The child user's initial password may have already been reset, or the child signin returned a different challenge.
- **Fix**: Check the `Error` column in the output CSV for details. Manually inspect the child account.

### `Features Enabled=false` — Lambda invocation failed

- **Cause**: AWS credentials not configured, wrong profile name, or Lambda function name incorrect.
- **Fix**: Check `profile` in `config.yaml`. Run `aws lambda list-functions --profile dev1 --region us-east-1` to verify access. To skip Lambda entirely, set `lambda.enabled: false` in `config.yaml`.

### `Features Enabled=false` — Lambda invocation failed

- **Cause**: AWS credentials not configured, wrong profile name, or Lambda function name incorrect.
- **Fix**: Check `profile` in `config.yaml`. Run `aws lambda list-functions --profile dev1 --region us-east-1` to verify access. To skip Lambda entirely, set `lambda.enabled: false` in `config.yaml`.

### `Status=FAILED` — "POST /auth/change-password returned 400" (min 8 chars, uppercase, lowercase, digit, special char).
- **Fix**: Update `Root Password` in the CSV to a compliant password (e.g. `Gamedayaws156!`) and re-run.

### `Tenant Status=FAILED` — tenant creation failed inline

- **Cause**: API error during tenant creation (e.g. duplicate tenant name, expired token).
- **Fix**: Check the `Tenant Error` column for details. Re-run using the fallback command: `python gameday.py create-tenants --csv users_output_<ts>.csv` — it skips rows where `Tenant Status=SUCCESS`.

### JWT token expired during a long run

- **Cause**: JWT tokens expire after ~1 hour. Long runs with many batches may exceed this.
- **Fix**: If you see `401` errors mid-run, note which rows failed, re-run the script (it will re-sign in for those rows), and the fresh signin at step 3 will obtain a new token automatically.

### Dry-run output looks correct but live run fails

- **Cause**: API behaviour differences between dev and the dry-run stub (e.g. missing fields in real responses).
- **Fix**: Check the `Error` column. Enable debug logging with `export LOG_LEVEL=DEBUG` and re-run.

---

## File Layout

```
GameDay/
├── gameday.py             # Main CLI script
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variable template
├── .env                   # Your local config (git-ignored, not committed)
├── sample_users.csv       # Starter CSV template
├── GameDayUserCreation.PRD  # Original requirements document
├── Notes.md               # Dev notes and curl examples
├── test-user-creation.sh  # Manual curl testing script
└── results/               # (optional) place output files here
```

---

## Quick Start Checklist

- [ ] `cd GameDay/`
- [ ] `pip install -r requirements.txt`
- [ ] `cp .env.example .env` and edit `BASE_URL`, `CAPTCHA_CODE`
- [ ] Prepare CSV with user data (`sample_users.csv` as template)
- [ ] `python gameday.py create-users --csv my_users.csv --dry-run` (validate first)
- [ ] `python gameday.py create-users --csv my_users.csv`
- [ ] Verify emails when prompted (see [Section 7](#7-manual-step--email-verification))
- [ ] Check `users_output_<ts>.csv` — confirm all rows are `Status=SUCCESS` and `Tenant Status=SUCCESS`
- [ ] If any row has `Tenant Status=FAILED`, re-run: `python gameday.py create-tenants --csv users_output_<ts>.csv`
