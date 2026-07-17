# AWS Resource Creation Framework for performance testing

A Python framework that provisions and tears down large numbers of AWS resources via CloudFormation. Resources are automatically split across multiple stacks to stay within CloudFormation's 500-resource-per-stack limit.

## Prerequisites

- Python 3.10+
- AWS credentials configured (`aws configure` or environment variables)
- An existing S3 bucket for template uploads (required when a stack template exceeds 40 KB)

## Setup

```bash
pip install -r requirements.txt
```

## Configuration

Edit `config.yaml` before running any command.

```yaml
global:
  prefix: mc-perftest       # prepended to every resource name
  region: us-east-1
  s3_bucket: my-bucket      # used for large template uploads

resources:
  sns_topic:
    enabled: true
    count: 4500
  sqs_queue:
    enabled: true
    count: 4500
  # ... more resource types
```

| Field | Description |
|---|---|
| `prefix` | Name prefix for all resources and stacks |
| `region` | AWS region to deploy into |
| `s3_bucket` | S3 bucket for oversized CFN templates (must already exist) |
| `enabled` | `true` to include this type in create/scale operations |
| `count` | Total number of resources to provision (`0` = nothing to do) |

## Commands

### `create` — provision all enabled resources

```bash
python3 framework.py create
```

Reads `config.yaml` and creates CloudFormation stacks for every resource type where `enabled: true` and `count > 0`. Already-deployed stacks are skipped (safe to re-run).

**Example output**
```
[sns_topic] 4500 resources -> 23 stack(s)
  Submitting 23 stack(s) ...
    + mc-perftest-sns-000001-000200: created
    + mc-perftest-sns-000201-000400: created
    ...
    Waiting for 23 stack(s)...............
  All 23 stack(s) created successfully.

[lambda_function] 800 resources -> 8 stack(s)
  Creating mc-perftest-lambda-roles ...
    Waiting for 1 stack(s)..
  Submitting 8 stack(s) ...
    ...
```

---

### `scale` — add more resources to a deployed type

```bash
python3 framework.py scale --resource <type> --add <n>
```

Finds the highest-indexed resource already deployed for that type, then creates new stacks starting from the next index. Updates `count` in `config.yaml` when done.

```bash
# Add 500 more SNS topics on top of existing 4500 → new total: 5000
python3 framework.py scale --resource sns_topic --add 500

# Add 200 more Lambda functions
python3 framework.py scale --resource lambda_function --add 200
```

---

### `delete` — remove stacks

Delete a single resource type:
```bash
python3 framework.py delete --resource sns_topic
python3 framework.py delete --resource sqs_queue
python3 framework.py delete --resource lambda_function
```

Delete everything (all resource stacks + the Lambda roles stack):
```bash
python3 framework.py delete --all
```

---

### `status` — show what is deployed

```bash
python3 framework.py status
```

**Example output**
```
Resource                    Stacks   Resources  Status
----------------------------------------------------------------------
lambda_function                  8         800  CREATE_COMPLETE
s3_bucket                        1          50  CREATE_COMPLETE
sns_topic                       23        4500  CREATE_COMPLETE
sqs_queue                       23        4500  CREATE_COMPLETE

  Lambda roles stack (mc-perftest-lambda-roles): CREATE_COMPLETE

Total resource stacks: 55
```

---

## Resource coverage

| Resource | Generator | Notes |
|---|---|---|
| `sns_topic` | Yes | 200 per stack |
| `sqs_queue` | Yes | 200 per stack |
| `s3_bucket` | Yes | 100 per stack; names must be globally unique |
| `lambda_function` | Yes | 100 per stack; requires a shared IAM role stack |
| `dynamodb_table` | Yes | 100 per stack; PAY_PER_REQUEST billing |
| `iam_policy` | Yes | 50 per stack |
| `iam_role` | Yes | 50 per stack |
| `iam_user` | Yes | 50 per stack |
| `auto_scaling_group` | No | Enable once a generator is added |
| `vpc` / `subnet` / `security_group` / `route_table` / `internet_gateway` / `vpc_peering_connection` / `network_acl` / `target_group` | No | Enable once generators are added |

## How stacks are named

```
{prefix}-{short}-{start:06d}-{end:06d}

Examples:
  mc-perftest-sns-000001-000200
  mc-perftest-sqs-002801-003000
  mc-perftest-lambda-000101-000200
  mc-perftest-lambda-roles          ← shared IAM role for Lambda
```

The start/end indices are encoded in the stack name so the framework can detect gaps and resume without extra state files.

## Lambda dependency

Lambda functions need an execution role. The framework automatically creates a `{prefix}-lambda-roles` stack that exports the role ARN, and each Lambda batch stack imports it. When running `delete --all`, Lambda batch stacks are deleted first, then the roles stack.

## Large templates and S3

CloudFormation's inline template limit is 51 KB. Any template that exceeds 40 KB is automatically uploaded to `s3_bucket` under the `cfn-templates/` prefix before deployment. The S3 bucket must already exist.

