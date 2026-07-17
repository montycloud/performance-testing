"""
Top-level command implementations: create, scale, delete, status.
Each function receives the loaded config dict and operates on AWS.
"""

import math
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.exceptions import ClientError

from cfn.constants import (
    BATCH_SIZE,
    FAILED_STATES,
    HEALTHY_STATES,
    RESOURCE_SHORT,
)
from cfn.config import save_config
from cfn.stack_names import make_stack_name, parse_stack_name
from cfn.stack_ops import (
    batch_delete,
    deploy_one,
    ensure_lambda_roles_stack,
    list_framework_stacks,
    wait_for_create,
)
from cfn.templates import TEMPLATE_GENERATORS


def cmd_create(cfg: dict) -> None:
    prefix = cfg["global"]["prefix"]
    region = cfg["global"]["region"]
    s3_bucket = cfg["global"]["s3_bucket"]
    cfn = boto3.client("cloudformation", region_name=region)
    s3_client = boto3.client("s3", region_name=region)

    for resource_type, settings in cfg["resources"].items():
        if not settings.get("enabled") or settings.get("count", 0) == 0:
            continue
        if resource_type not in TEMPLATE_GENERATORS:
            print(f"[SKIP] {resource_type}: no template generator")
            continue

        total = settings["count"]
        batch_sz = BATCH_SIZE[resource_type]
        short = RESOURCE_SHORT[resource_type]
        num_batches = math.ceil(total / batch_sz)
        print(f"\n[{resource_type}] {total} resources -> {num_batches} stack(s)")

        if resource_type == "lambda_function":
            ensure_lambda_roles_stack(cfn, s3_client, prefix, s3_bucket, region)

        # Separate existing stacks into healthy (skip) and failed (purge + retry)
        existing = list_framework_stacks(cfn, prefix, resource_type)
        healthy_names = {s["name"] for s in existing if s["status"] in HEALTHY_STATES}
        failed_stacks = [s for s in existing if s["status"] in FAILED_STATES]

        if failed_stacks:
            print(f"  Found {len(failed_stacks)} failed stack(s) — cleaning up before retry:")
            for s in failed_stacks:
                print(f"    {s['name']}  [{s['status']}]")
            batch_delete(cfn, [s["name"] for s in failed_stacks])

        # One task per batch not yet successfully deployed
        tasks = []
        for batch_num in range(1, num_batches + 1):
            start = (batch_num - 1) * batch_sz + 1
            end = min(batch_num * batch_sz, total)
            stack_name = make_stack_name(prefix, short, start, end)
            if stack_name in healthy_names:
                continue
            template = TEMPLATE_GENERATORS[resource_type](prefix, start, end - start + 1)
            tasks.append((stack_name, template))

        if not tasks:
            print("  All batches already deployed.")
            continue

        print(f"  Submitting {len(tasks)} stack(s) ...")
        created, submit_errors = _submit_stacks(
            tasks, cfn, s3_client, prefix, s3_bucket, region, resource_type
        )

        if created:
            results = wait_for_create(cfn, created)
            cfn_fails = [n for n, s in results.items() if s != "CREATE_COMPLETE"]
            if cfn_fails:
                print(f"  CFN rollbacks ({len(cfn_fails)}): {cfn_fails}", file=sys.stderr)
                print("  Re-run `create` to retry failed batches.", file=sys.stderr)
            else:
                print(f"  All {len(created)} stack(s) created successfully.")

        if submit_errors:
            print(
                f"  {len(submit_errors)} stack(s) failed to submit — re-run `create` to retry.",
                file=sys.stderr,
            )

    print("\nDone.")


def cmd_scale(cfg: dict, resource_type: str, add: int) -> None:
    if resource_type not in cfg["resources"]:
        sys.exit(f"Unknown resource: {resource_type}")
    if resource_type not in TEMPLATE_GENERATORS:
        sys.exit(f"No template generator for: {resource_type}")

    prefix = cfg["global"]["prefix"]
    region = cfg["global"]["region"]
    s3_bucket = cfg["global"]["s3_bucket"]
    cfn = boto3.client("cloudformation", region_name=region)
    s3_client = boto3.client("s3", region_name=region)

    deployed = list_framework_stacks(cfn, prefix, resource_type)
    current_max = max((s["end"] for s in deployed), default=0)
    new_start = current_max + 1
    new_end = current_max + add
    batch_sz = BATCH_SIZE[resource_type]
    short = RESOURCE_SHORT[resource_type]

    print(f"[{resource_type}] Adding {add} resources (indices {new_start}-{new_end})")

    if resource_type == "lambda_function":
        ensure_lambda_roles_stack(cfn, s3_client, prefix, s3_bucket, region)

    tasks = []
    i = new_start
    while i <= new_end:
        end = min(i + batch_sz - 1, new_end)
        stack_name = make_stack_name(prefix, short, i, end)
        template = TEMPLATE_GENERATORS[resource_type](prefix, i, end - i + 1)
        tasks.append((stack_name, template))
        i += batch_sz

    print(f"  Submitting {len(tasks)} stack(s) ...")
    created, _ = _submit_stacks(
        tasks, cfn, s3_client, prefix, s3_bucket, region, resource_type
    )

    if created:
        wait_for_create(cfn, created)

    cfg["resources"][resource_type]["count"] = new_end
    save_config(cfg)
    print(f"\nUpdated config count for {resource_type}: {new_end}")


def cmd_delete(cfg: dict, resource_type: str | None, delete_all: bool) -> None:
    prefix = cfg["global"]["prefix"]
    region = cfg["global"]["region"]
    cfn = boto3.client("cloudformation", region_name=region)

    stacks = list_framework_stacks(cfn, prefix, None if delete_all else resource_type)
    names = [s["name"] for s in stacks]

    roles_stack = f"{prefix}-lambda-roles"
    has_roles_stack = False
    if delete_all:
        try:
            cfn.describe_stacks(StackName=roles_stack)
            has_roles_stack = True
        except ClientError:
            pass

    if not names and not has_roles_stack:
        print("No matching stacks found.")
        return

    all_names = names + ([roles_stack] if has_roles_stack else [])
    print(f"Deleting {len(all_names)} stack(s):")
    for n in all_names:
        print(f"  {n}")

    # Lambda batch stacks must go before the roles stack (cross-stack import dependency)
    lambda_stacks = [
        n for n in names
        if parse_stack_name(prefix, n) and
           parse_stack_name(prefix, n)["resource"] == "lambda_function"
    ]
    other_stacks = [n for n in names if n not in lambda_stacks]

    batch_delete(cfn, other_stacks)
    batch_delete(cfn, lambda_stacks)
    if has_roles_stack:
        batch_delete(cfn, [roles_stack])

    print("Done.")


def cmd_status(cfg: dict) -> None:
    prefix = cfg["global"]["prefix"]
    region = cfg["global"]["region"]
    cfn = boto3.client("cloudformation", region_name=region)

    stacks = list_framework_stacks(cfn, prefix)
    if not stacks:
        print("No framework stacks found.")
        return

    by_resource: dict[str, list] = {}
    for s in stacks:
        by_resource.setdefault(s["resource"], []).append(s)

    print(f"\n{'Resource':<26} {'Stacks':>7} {'Resources':>11}  Status")
    print("-" * 70)
    total_stacks = 0
    for rtype in sorted(by_resource):
        batches = by_resource[rtype]
        resource_count = sum(s["end"] - s["start"] + 1 for s in batches)
        statuses = ", ".join(sorted({s["status"] for s in batches}))
        print(f"{rtype:<26} {len(batches):>7} {resource_count:>11}  {statuses}")
        total_stacks += len(batches)

    roles_stack = f"{prefix}-lambda-roles"
    try:
        resp = cfn.describe_stacks(StackName=roles_stack)
        status = resp["Stacks"][0]["StackStatus"]
        print(f"\n  Lambda roles stack ({roles_stack}): {status}")
    except ClientError:
        pass

    print(f"\nTotal resource stacks: {total_stacks}")


# ---------------------------------------------------------------------------
# Shared submit helper
# ---------------------------------------------------------------------------

def _submit_stacks(
    tasks: list[tuple],
    cfn,
    s3_client,
    prefix: str,
    s3_bucket: str,
    region: str,
    resource_type: str,
) -> tuple[list[str], list[str]]:
    """Submit tasks in parallel. Returns (created_names, error_names)."""
    created: list[str] = []
    errors: list[str] = []

    def _deploy(args: tuple) -> tuple[str, str]:
        sn, tmpl = args
        return sn, deploy_one(cfn, s3_client, prefix, s3_bucket, region, sn, tmpl, resource_type)

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(_deploy, t): t[0] for t in tasks}
        for fut in as_completed(futs):
            sn = futs[fut]
            try:
                sn, result = fut.result()
                print(f"    {'+' if result == 'created' else '~'} {sn}: {result}")
                if result == "created":
                    created.append(sn)
            except Exception as exc:
                print(f"    ! {sn}: submit failed — {exc}", file=sys.stderr)
                errors.append(sn)

    return created, errors
