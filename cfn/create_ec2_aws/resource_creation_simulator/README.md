# Resource creation simulator

Standalone helper (no MontyCloud internal imports) that generates the JSON
events MontyCloud's discovery pipeline would see for real EC2 instance
activity — **no real AWS resources are created**. See also
[EC2_EVENT_SIMULATION_PLAN.md](EC2_EVENT_SIMULATION_PLAN.md) for the design
rationale and open follow-ups.

It fabricates the chain:
```
Native EventBridge event (state-change or tag-change) OR CloudTrail record
  -> SNS Notification envelope (as forwarded from the customer account)
  -> SNS -> SQS fan-out, which triggers the Lambda with a Records array
  -> Lambda event shape expected by mc_helpers/mc_queue_handler.py::queue_wrapper,
     which calls api/modules/services/sns_subsription/handler.py::task() per record
```

## Modes

| `--mode` | What it builds | Reaches a domain handler? |
|---|---|---|
| `state-change` (default) | Native `EC2 Instance State-change Notification` (`pending`/`running`/`stopping`/`stopped`/`shutting-down`/`terminated`) for a **real, existing** `--instance-id` | Yes — drives `InstanceStateChanged` |
| `tag-change` | Native `Tag Change on Resource` (`resource-type: instance`) for a **real, existing** `--instance-id` with `--tag-keys` | Yes — drives `InstanceTagChanged` |
| `cloudtrail` | Raw `RunInstances` CloudTrail record for `--count` fabricated instances | No — intentionally dropped by the noise filter; useful only to exercise/prove that filter |

`state-change` and `tag-change` require a real instance id that already exists in the
MontyCloud-onboarded AWS account, because the downstream consumer looks up/refreshes the
instance by that id (and, for brand-new instances, needs real AWS API data to populate
details) — a fabricated id won't create or discover anything.

Run:
```bash
# Default mode: simulate an existing instance being (re)discovered as running
# (pending->running). Use --action delete for shutting-down->terminated, or --state
# for a single specific state.
python simulate_ec2_discovery_event.py --account-number 123456789012 --region us-east-1 \
  --instance-id i-0abc123456789def0 --action create

# Simulate a tag change on an existing instance
python simulate_ec2_discovery_event.py --account-number 123456789012 --region us-east-1 \
  --instance-id i-0abc123456789def0 --mode tag-change \
  --tag-keys "Patch Group=HRAPP,Environment=prod"

# Legacy: generate the (noise-filtered) RunInstances CloudTrail payload for N fabricated instances
python simulate_ec2_discovery_event.py --account-number 123456789012 --region us-east-1 \
  --count 10 --mode cloudtrail

# Also invoke a real deployed sns_subsription Lambda (e.g. in a dev/stage account) with it
# Function name follows serverless.yml's "service-stage-function" convention; the service
# is "sns-subscription-v2" (api/modules/services/sns_subsription/serverless.yml), not the
# misspelled "sns_subsription" directory name.
python simulate_ec2_discovery_event.py --account-number 123456789012 --region us-east-1 \
  --instance-id i-0abc123456789def0 --action create \
  --invoke-lambda --function-name sns-subscription-v2-dev1-task --profile dev1-account-user
```

If you get `ResourceNotFoundException: Function not found`, confirm the exact deployed
name first:
```bash
aws lambda list-functions --profile <profile> --region <region> \
  --query "Functions[?contains(FunctionName, 'sns-subscription')].FunctionName"
```

## Load/perf-test helpers

For driving the performance-test profiles (burst, spike, full-lifecycle churn, soak), the
script adds concurrency, scenario replay, and steady-rate options on top of the base modes
above. See [EC2_EVENT_SIMULATION_PLAN.md](EC2_EVENT_SIMULATION_PLAN.md) for the design
rationale behind these.

| Flag | Purpose |
|---|---|
| `--concurrency N` | Dispatch up to N events in parallel instead of sequentially (default: 1) |
| `--ramp-seconds S` | Spread dispatch start times evenly across S seconds instead of firing all at once (needs `--concurrency > 1`) |
| `--scenario-file PATH` | Replay an ordered mix of `state-change`/`tag-change` steps per instance id — see [scenario_example.txt](scenario_example.txt) |
| `--step-delay-seconds S` | Delay between steps within one instance's `--scenario-file` sequence |
| `--rate-per-second R` + `--duration-seconds D` | Soak mode: sustain R events/sec (cycling through instance ids) for D seconds, using `--mode` to pick the event type |

```bash
# Burst/spike: fire state-change events for many real instance ids concurrently, ramped over 30s
python simulate_ec2_discovery_event.py --account-number 123456789012 --region us-east-1 \
  --instance-ids-file ids.txt --mode state-change --action create \
  --concurrency 20 --ramp-seconds 30 --invoke-lambda --function-name sns-subscription-v2-dev1-task --profile dev1

# Full-lifecycle churn: replay a mixed state-change/tag-change sequence per instance
python simulate_ec2_discovery_event.py --account-number 123456789012 --region us-east-1 \
  --instance-ids-file ids.txt --scenario-file scenario_example.txt --step-delay-seconds 5 \
  --concurrency 10 --invoke-lambda --function-name sns-subscription-v2-dev1-task --profile dev1

# Soak: sustain a steady rate of tag-change events across a pool of instances for 10 minutes
python simulate_ec2_discovery_event.py --account-number 123456789012 --region us-east-1 \
  --instance-ids-file ids.txt --mode tag-change --tag-keys "Environment=stage" \
  --rate-per-second 1 --duration-seconds 600 --invoke-lambda --function-name sns-subscription-v2-dev1-task --profile dev1
```

Every invocation across every mode also appends one row to
`generated_events/run-summary-<run-id>.csv` (`timestamp, event_type, instance_id,
lambda_status_code, lambda_duration_ms, throttle_retries`) so you can line up what was sent
against CloudWatch Logs Insights afterwards.

Output payloads are written under `generated_events/<run-id>-<instance-id>[-<state>|-tagchange].json`.
Note: `--account-number` must be an account already onboarded in MontyCloud (i.e.
has a `CustomerAccount` DB record) for a real Lambda invoke to resolve a
`CustomerId` — otherwise `get_context()` in
`sns_subscription_shared/__init__.py` raises "No customer found for account id".

### High-throughput dispatch (large volume runs)

| Flag | Purpose |
|---|---|
| `--async-invoke` | Invoke with `InvocationType=Event` (fire-and-forget) instead of `RequestResponse`, so dispatch doesn't block waiting for the downstream Lambda to finish executing. By far the biggest throughput lever for large runs. Per-call `lambda_status_code`/`lambda_duration_ms` then only reflect submission (202 = accepted, not "succeeded") |

Throttling (`TooManyRequestsException`) is retried automatically with exponential
backoff + jitter (up to 5 attempts); the retry count for each event is recorded in the
run-summary CSV's `throttle_retries` column.

One boto3 Lambda client is cached per (`--profile`, `--lambda-region`) and shared/reused
across all dispatch threads (clients are thread-safe, Sessions aren't), with its
connection pool sized to `--concurrency` — avoids both the overhead of creating a new
`Session`+`client` per call and pool-exhaustion stalls under high thread counts.

GCP/Azure event shapes are not implemented here — this only models the
AWS CloudTrail/EventBridge EC2 shapes (state-change, tag-change, RunInstances).

## Multi-account tag-change fan-out (`simulate_multi_account_tag_change.py`)

Wraps the base script for the common "N instances across M customer accounts" case —
reuses its event-building/dispatch logic, nothing duplicated. Takes a CSV mapping
(`account_number,region,instance_id`) instead of a single `--account-number`/`--region`.

```bash
python simulate_multi_account_tag_change.py --mapping-file accounts_instances.csv \
  --tag-keys "perftesttag=loadtest-26oct" \
  --invoke-lambda --function-name sns-subscription-v2-stg1-task \
  --profile stg1 --lambda-region us-east-2
```

| Flag | Purpose |
|---|---|
| `--separate-events` | Fire ONE `Tag Change on Resource` event PER tag in `--tag-keys` (changed-tag-keys scoped to just that tag, `tags` accumulating progressively) instead of one event carrying all tags at once — use this to generate large discrete event volumes (e.g. 12K events = 160 instances x 75 tags) |
| `--async-invoke` | Same as the base script — fire-and-forget Lambda invokes, the main lever for large runs |
| `--concurrency N` | Max concurrent event DISPATCHES (not instances) — the script precomputes every instance's event(s) first, then flattens all instances' events into one pool, so this can safely be set much higher than the instance count (e.g. 150-300) |
| `--max-invokes-per-second R` | Optional global rate cap across all threads — a safety valve when the target Lambda's account/reserved concurrency limit is unknown |
| `--skip-existing-tags-fetch` | Skip the per-instance `describe_tags` prefetch (falls back to `--tag-keys` only, not matching AWS's full-tag-set contract) |

Example — 75 tags x ~160 instances (~12K discrete events), async + rate-capped:
```bash
python simulate_multi_account_tag_change.py --mapping-file accounts_instances.csv \
  --tag-keys "k1=v1,k2=v2,...,k75=v75" \
  --separate-events --async-invoke \
  --invoke-lambda --function-name sns-subscription-v2-stg1-task \
  --profile stg1 --lambda-region us-east-2 \
  --concurrency 300 --max-invokes-per-second 250
```

Before raising `--concurrency`/`--max-invokes-per-second` further, check the real limits:
```bash
aws lambda get-function-concurrency --function-name sns-subscription-v2-stg1-task --profile stg1 --region us-east-2
aws service-quotas get-service-quota --service-code lambda --quota-code L-B99A9384 --profile stg1 --region us-east-2
```
