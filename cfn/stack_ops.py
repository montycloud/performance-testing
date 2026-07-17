"""
Low-level CloudFormation and S3 operations:
  - uploading templates to S3
  - creating / waiting on stacks
  - listing / deleting framework stacks
  - ensuring the Lambda roles prerequisite stack exists
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from botocore.exceptions import ClientError

from cfn.constants import (
    FAILED_STATES,
    INLINE_SIZE_LIMIT,
    MAX_WORKERS,
    NEEDS_NAMED_IAM,
    S3_KEY_PREFIX,
)
from cfn.stack_names import make_stack_name, parse_stack_name
from cfn.templates import lambda_roles_template


# ---------------------------------------------------------------------------
# S3 template upload
# ---------------------------------------------------------------------------

def upload_template(s3_client, bucket: str, region: str, key: str, body: str) -> str:
    s3_client.put_object(Bucket=bucket, Key=key, Body=body.encode())
    return f"https://s3.{region}.amazonaws.com/{bucket}/{key}"


# ---------------------------------------------------------------------------
# Stack create
# ---------------------------------------------------------------------------

def deploy_one(
    cfn,
    s3_client,
    prefix: str,
    s3_bucket: str,
    region: str,
    stack_name: str,
    template: dict,
    resource_type: str,
) -> str:
    """Submit a single stack create. Returns 'created' | 'exists' | raises."""
    body = json.dumps(template)
    capabilities = ["CAPABILITY_NAMED_IAM"] if resource_type in NEEDS_NAMED_IAM else []
    kwargs: dict = dict(StackName=stack_name, Capabilities=capabilities)
    if len(body.encode()) > INLINE_SIZE_LIMIT:
        key = f"{S3_KEY_PREFIX}/{stack_name}.json"
        kwargs["TemplateURL"] = upload_template(s3_client, s3_bucket, region, key, body)
    else:
        kwargs["TemplateBody"] = body
    try:
        cfn.create_stack(**kwargs)
        return "created"
    except ClientError as e:
        if e.response["Error"]["Code"] == "AlreadyExistsException":
            return "exists"
        raise


# ---------------------------------------------------------------------------
# Waits
# ---------------------------------------------------------------------------

def wait_for_create(cfn, stack_names: list[str]) -> dict[str, str]:
    """Block until every stack leaves *_IN_PROGRESS. Returns {name: final_status}."""
    pending = set(stack_names)
    results: dict[str, str] = {}
    print(f"    Waiting for {len(pending)} stack(s)", end="", flush=True)
    while pending:
        time.sleep(10)
        print(".", end="", flush=True)
        done = set()
        for name in list(pending):
            try:
                status = cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
            except ClientError:
                status = "DELETED"
            if "IN_PROGRESS" not in status:
                results[name] = status
                done.add(name)
        pending -= done
    print()
    return results


def wait_for_delete(cfn, stack_names: list[str]) -> None:
    """Block until every stack is gone from the CFN API."""
    pending = set(stack_names)
    print(f"  Waiting for {len(pending)} deletion(s)", end="", flush=True)
    while pending:
        time.sleep(10)
        print(".", end="", flush=True)
        done = set()
        for name in list(pending):
            try:
                cfn.describe_stacks(StackName=name)
            except ClientError as e:
                if "does not exist" in str(e):
                    done.add(name)
        pending -= done
    print()


# ---------------------------------------------------------------------------
# Parallel delete
# ---------------------------------------------------------------------------

def batch_delete(cfn, stack_names: list[str]) -> None:
    """Initiate parallel deletes and block until every stack is gone."""
    if not stack_names:
        return
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(cfn.delete_stack, StackName=n): n for n in stack_names}
        for fut in as_completed(futs):
            n = futs[fut]
            try:
                fut.result()
                print(f"  delete initiated: {n}")
            except ClientError as e:
                print(f"  error deleting {n}: {e}", file=sys.stderr)
    wait_for_delete(cfn, stack_names)


# ---------------------------------------------------------------------------
# Stack listing
# ---------------------------------------------------------------------------

def list_framework_stacks(
    cfn, prefix: str, resource_type: str | None = None
) -> list[dict]:
    """Return all framework resource stacks, optionally filtered by resource_type.

    Each entry: {name, resource, short, start, end, status}
    """
    stacks = []
    status_filter = [
        "CREATE_COMPLETE", "CREATE_IN_PROGRESS", "CREATE_FAILED",
        "UPDATE_COMPLETE", "UPDATE_IN_PROGRESS",
        "ROLLBACK_COMPLETE", "UPDATE_ROLLBACK_COMPLETE",
    ]
    paginator = cfn.get_paginator("list_stacks")
    for page in paginator.paginate(StackStatusFilter=status_filter):
        for s in page["StackSummaries"]:
            parsed = parse_stack_name(prefix, s["StackName"])
            if parsed is None:
                continue
            if resource_type and parsed["resource"] != resource_type:
                continue
            parsed["status"] = s["StackStatus"]
            stacks.append(parsed)
    return stacks


# ---------------------------------------------------------------------------
# Lambda roles prerequisite stack
# ---------------------------------------------------------------------------

def ensure_lambda_roles_stack(
    cfn, s3_client, prefix: str, s3_bucket: str, region: str
) -> None:
    """Create the shared Lambda execution role stack if it does not exist.

    If the stack is in a failed state it is deleted and re-created automatically.
    Blocks until the stack reaches CREATE_COMPLETE.
    """
    stack_name = f"{prefix}-lambda-roles"
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
        status = resp["Stacks"][0]["StackStatus"]
        if status == "CREATE_COMPLETE":
            return
        if "IN_PROGRESS" in status:
            print(f"  Waiting for existing {stack_name}...")
            results = wait_for_create(cfn, [stack_name])
            if results[stack_name] == "CREATE_COMPLETE":
                return
            status = results[stack_name]
        if status in FAILED_STATES:
            print(f"  {stack_name} is in {status} — deleting and retrying ...")
            cfn.delete_stack(StackName=stack_name)
            wait_for_delete(cfn, [stack_name])
        else:
            print(f"  Lambda roles stack is in unexpected state: {status}", file=sys.stderr)
            sys.exit(1)
    except ClientError as e:
        if "does not exist" not in str(e):
            raise

    print(f"  Creating {stack_name} ...")
    template = lambda_roles_template(prefix)
    body = json.dumps(template)
    kwargs: dict = dict(
        StackName=stack_name,
        Capabilities=["CAPABILITY_NAMED_IAM"],
        Tags=[{"Key": "framework:lambda_roles", "Value": "true"}],
    )
    if len(body.encode()) > INLINE_SIZE_LIMIT:
        key = f"{S3_KEY_PREFIX}/{stack_name}.json"
        kwargs["TemplateURL"] = upload_template(s3_client, s3_bucket, region, key, body)
    else:
        kwargs["TemplateBody"] = body

    cfn.create_stack(**kwargs)
    results = wait_for_create(cfn, [stack_name])
    if results[stack_name] != "CREATE_COMPLETE":
        print(f"  ERROR: {stack_name} => {results[stack_name]}", file=sys.stderr)
        sys.exit(1)
