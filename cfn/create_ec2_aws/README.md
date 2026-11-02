# ec2_lockdown.py

Creates one or more (per `--count`) locked-down, free-tier EC2 instances in
each AWS account listed in `accounts.txt`, stops them immediately, and can
tag/stop/terminate them later. Standalone boto3 script — independent of the
CloudFormation batch-stack framework in `cfn/framework.py`.

Built for the compute-tag-change performance test ([Compute_performance.prd](Compute_performance.prd)):
spin up placeholder instances across many accounts, then add/remove tags on
them (all at once or ramped up) to verify tag-change events reach downstream
consumers, then tear everything down.

## What "locked down" means

- No key pair (no PEM file) — nobody can SSH in
- No public IP — not reachable from the internet
- Security group with **zero** inbound and **zero** outbound rules
- No IAM instance profile — no SSM Session Manager access
- Instance metadata service (IMDS) disabled
- EC2 Serial Console access disabled for the account/region
- Smallest root disk (8 GiB gp3), **encrypted**, deleted on terminate
- Stopped as soon as it reaches `running`

## Files

- `accounts.txt` (input, CSV, header required):
  ```
  accountNumber,region_to_create_ec2,profile_in_aws
  330759081013,us-east-1,330759081013
  ```
  Credentials come from `~/.aws/credentials` / `~/.aws/config` via the profile
  name. The script verifies the profile's STS identity matches the account
  number before doing anything, and skips the row otherwise.

- `instances.csv` (generated manifest, source of truth for instance ids):
  ```
  account,region,instance_id,sg_id,created_at
  ```
  Used instead of relying solely on EC2 tags, since the tag-change perf test
  itself adds/removes tags on these instances.

## Usage

```bash
# create
python3 ec2_lockdown.py create                                  # create + stop, 1 per account, all accounts in parallel
python3 ec2_lockdown.py create --count 10                        # create + stop, 10 per account
python3 ec2_lockdown.py create --only 330759081013               # only this account
python3 ec2_lockdown.py create --instance-type t3.micro          # force an instance type
python3 ec2_lockdown.py create --ramp-size 5 --ramp-delay 60      # 5 accounts in parallel per batch, 60s between batches
python3 ec2_lockdown.py create --dry-run                         # validate only, no real resources

# status
python3 ec2_lockdown.py status                                   # show instances
python3 ec2_lockdown.py status --show-tags                       # also print each instance's tags
python3 ec2_lockdown.py status --only 330759081013 151963020718  # limit to specific accounts

# stop
python3 ec2_lockdown.py stop                                      # stop any running ones (all accounts)
python3 ec2_lockdown.py stop --only 330759081013                 # just this account

# terminate
python3 ec2_lockdown.py terminate                                 # terminate + delete SGs (asks to confirm)
python3 ec2_lockdown.py terminate --yes                           # no confirmation prompt
python3 ec2_lockdown.py terminate --only 330759081013 --instance-id i-0abc123   # one specific instance
python3 ec2_lockdown.py terminate --only 330759081013 --instance-id i-0abc i-0def --region us-west-2

# tag
python3 ec2_lockdown.py tag --add-tag Env=perf                                  # add a tag everywhere
python3 ec2_lockdown.py tag --add-tag Env=perf --add-tag Phase=rampup           # multiple tags at once
python3 ec2_lockdown.py tag --remove-tag Old                                    # remove a tag key
python3 ec2_lockdown.py tag --add-tag Env=perf --only 330759081013              # limit to one account
python3 ec2_lockdown.py tag --add-tag Env=perf --ramp-size 2 --ramp-delay 60    # 2 accounts at a time, parallel within each batch, 60s between batches
```

## Options reference

| Flag | Applies to | Meaning |
|---|---|---|
| `--accounts FILE` | all | accounts file (default: `accounts.txt`) |
| `--manifest FILE` | all | instance manifest file (default: `instances.csv`) |
| `--only ACCT [ACCT ...]` | all | run only for these account numbers |
| `--dry-run` | all | ask AWS to validate the calls without making real changes |
| `--instance-type TYPE` | `create` | force a type, e.g. `t3.micro` (default: auto-detect free tier) |
| `--count N` | `create` | instances to create per account (default: `1`); incremental — only creates the shortfall if some already exist |
| `--instance-id ID [ID ...]` | `terminate` | terminate exactly these instance ids (requires a single `--only` account) |
| `--region REGION` | `terminate` | override the region for `--instance-id` (defaults to the `accounts.txt` row) |
| `--yes` | `terminate` | skip the confirmation prompt |
| `--show-tags` | `status` | also print each instance's current tags |
| `--add-tag KEY=VALUE` | `tag` | tag to add (repeatable) |
| `--remove-tag KEY` | `tag` | tag key to remove (repeatable) |
| `--ramp-size N` | `create`, `tag` | apply to N accounts per batch, in parallel within each batch (default: all accounts at once, in parallel) |
| `--ramp-delay SECONDS` | `create`, `tag` | seconds to wait between ramp-up batches (default: `30`) |

Run `python3 ec2_lockdown.py -h` for the live `--help` output.

## Status

Implemented, smoke-tested, and run for real across all 10 accounts (multiple
instances per account via `--count`, tagging via `--add-tag`/`--remove-tag`).

Commands to Generate Tag and Run the execution for 24K Tags

`Tag generation: TAGS=$(python3 -c "print(','.join(f'perftestkey{i}=perftestvalue{i}' for i in range(1, 151)))")
`


`python simulate_multi_account_tag_change.py \
--mapping-file accounts_instances.csv \
--tag-keys "$TAGS" \
--separate-events \
--skip-existing-tags-fetch \
--invoke-lambda --function-name sns-subscription-v2-stg1-task \
--profile stg1 --lambda-region us-east-2 \
--concurrency 900 \
--max-invokes-per-second 900`