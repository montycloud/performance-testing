# MontyCloud performance testing

Load tests and test-data generators for the MontyCloud platform.

The repo is mid-restructure onto one shared package, `mcperf`. Migrated tools
live under `mcperf/` (code) and `scenarios/` (what you run, with its config).
The rest still run from their original folders until their phase lands.

## Setup

Python 3.10+. From the repo root:

```bash
python3 -m venv .venv && source .venv/bin/activate
```

```bash
pip install -e ".[all]"
```

Copy `scenarios/api/users.csv.example` to `scenarios/api/users.csv` and fill in
real test users. It is gitignored; never commit credentials.

## Tools

| Tool | Run | Code | Status |
|---|---|---|---|
| AWS resource generator (CloudFormation) | `python -m mcperf.cfn <create\|scale\|delete\|status>` | `mcperf/cfn/` · [README](mcperf/cfn/README.md) | migrated |
| Inventory summary load test | `locust -f scenarios/api/inventory/locustfile.py` | `mcperf/api/inventory.py` | migrated (pilot) |
| Main journey, Cost Dashboard, Day2SDK load tests | see [API Performance test/README.md](API%20Performance%20test/README.md) | `API Performance test/` | phase 3 |
| Security Bot rescan test | see [Security_bot/README.md](Security_bot/README.md) | `Security_bot/` | phase 5 (moves under `mcperf/api/`) |
| Knowledge Base upload test | see [knowledgebase/loadtest/README.md](knowledgebase/loadtest/README.md) | `knowledgebase/loadtest/` | phase 5 |
| GameDay user provisioning | archived | `archive/gameday/` | unmaintained; tag `archive/gameday-v1` |

## Layout

```
mcperf/            library: reusable, tested, no config or data files
  core/            config, credentials, redaction (no Locust/boto3 imports)
  locust/          MontyCloudUser base class, batches, sign-in/GET helpers, run hooks
  reporting/       custom HTML report from Locust CSV stats
  api/             one flow module per product area
  cfn/             CloudFormation resource generator
scenarios/         thin entry points + their config.yaml and data
tests/             pytest; mirrors mcperf/
archive/           unmaintained tools kept for reference
```

## Adding a Locust scenario

1. `mcperf/api/<flow>.py`: a `<Flow>Config(ScenarioConfig)` for its own config
   section, and a `<Flow>User(MontyCloudUser)` that implements only `run_flow()`.
   Prefix request names with `[<Flow>]`.
2. `scenarios/api/<flow>/locustfile.py`: one `register_scenario(...)` call, plus
   its `config.yaml` (sections `api`, `load`, `report`, `<flow>`).
3. A test in `tests/`. `tests/scenarios/test_inventory_e2e.py` shows how to run a
   scenario against a local mock API.

`mcperf/api/inventory.py` and `scenarios/api/inventory/` are the reference.

## Tests

```bash
pytest
```

```bash
ruff check mcperf tests scenarios
```
