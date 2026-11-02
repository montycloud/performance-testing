#!/usr/bin/env python3
"""Generate synthetic EC2 discovery/rediscovery events — no real AWS resources touched.

IMPORTANT — why the old `--mode cloudtrail` payload never triggered rediscovery:
`RunInstances`/`TerminateInstances` on `ec2.amazonaws.com` are explicitly listed in
`is_noise()` (api/modules/services/sns_subsription/handler.py, see also
`CLOUD_TRAIL_NOISY_EVENT_NAMES` in observability/noise_detector.py) as high-frequency
CloudTrail noise, so the Lambda drops that event before any domain's `can_handle()`
ever runs. A real `aws ec2 run-instances` call *does* generate that noisy CloudTrail
event too — but what actually drives MontyCloud's rediscovery is a second, unrelated
EventBridge event that EC2 emits natively (not via CloudTrail):

    real state transition (pending -> running, or shutting-down -> terminated)
      -> native EventBridge event (detail-type="EC2 Instance State-change Notification",
         source="aws.ec2", detail={"instance-id": ..., "state": ...})
      -> customer-account EventBridge rule forwards it to an SNS topic
      -> SNS topic fans out to an SQS queue, which triggers the Lambda
      -> api/modules/services/sns_subsription/handler.py `task()` Lambda consumes it,
         is_noise() lets it through (detail-type != "AWS API Call via CloudTrail"),
         and the `EC2_INSTANCE_STATE` branch publishes an "InstanceStateChanged" event
         that the compute/inventory consumers use to (re)fetch and upsert the instance.

`--mode state-change` (default) builds that second event shape instead. Because the
downstream consumer looks up/refreshes the instance by its real `instance-id` (and,
for brand-new instances, ultimately needs real AWS API data to populate details), you
should pass `--instance-id` for an instance that actually exists in the onboarded AWS
account (e.g. one you already created/stopped) rather than a fabricated id — that lets
you replay "created"/"running" or "terminated" rediscovery without creating a new
resource. `--mode cloudtrail` is kept only for exercising/testing the noise filter
itself; it will NOT reach any domain handler on a real Lambda.

`--mode tag-change` builds the native `Tag Change on Resource` event (source `aws.tag`,
detail['resource-type']="instance") that drives handler.py's `TAG_CHANGE_ON_RESOURCE`
branch and publishes "InstanceTagChanged" — same noise-filter-bypass reasoning as
state-change applies (not a CloudTrail event, so is_high_frequency_cloudtrail_noise()
never matches it). Pass the new/changed tags via --tag-keys (e.g. --tag-keys Name=demo,Env=prod);
`changed-tag-keys` reports exactly those keys. The event's `tags` field is built by fetching the
instance's EXISTING tags via EC2 describe_tags and merging in --tag-keys, matching AWS's real
contract where `tags` is always the full current tag set, not just the delta — this matters because
Compute's InstanceHistory.New is written directly from this field. Use --skip-existing-tags-fetch to
opt out (falls back to --tag-keys only) if you don't have/need live AWS read access.

By default this script only prints/saves the payload(s) — nothing is sent anywhere.
Use --invoke-lambda only if you intentionally want to feed a payload into a real
deployed sns_subsription Lambda (e.g. in a dev/stage account you own) to exercise the
real ingestion code path end to end.

Usage:
    # Simulate an existing instance i-0abc123... transitioning to "running"
    # (use --action create for pending->running, --action delete for shutting-down->terminated,
    # or --state to send a single specific state)
    python simulate_ec2_discovery_event.py --account-number 123456789012 \\
        --region us-east-1 --instance-id i-0abc123456789def0 --action create

    # Also invoke a real (dev/stage) sns_subsription Lambda with it
    # Function name follows serverless.yml's "service-stage-function" convention:
    # service is "sns-subscription-v2" (see api/modules/services/sns_subsription/serverless.yml)
    python simulate_ec2_discovery_event.py --account-number 123456789012 \\
        --region us-east-1 --instance-id i-0abc123456789def0 --action create \\
        --invoke-lambda --function-name sns-subscription-v2-dev1-task --profile dev1-account-user

    # Legacy: generate the (noise-filtered) RunInstances CloudTrail payload
    python simulate_ec2_discovery_event.py --account-number 123456789012 \\
        --region us-east-1 --count 10 --mode cloudtrail

    # Simulate a tag change on an existing instance
    python simulate_ec2_discovery_event.py --account-number 123456789012 \\
        --region us-east-1 --instance-id i-0abc123456789def0 --mode tag-change \\
        --tag-keys "Patch Group=HRAPP,Environment=prod"

    # Burst/spike: fire state-change events for many real instance ids concurrently, ramped over 30s
    python simulate_ec2_discovery_event.py --account-number 123456789012 --region us-east-1 \\
        --instance-ids-file ids.txt --mode state-change --action create \\
        --concurrency 20 --ramp-seconds 30 --invoke-lambda --function-name sns-subscription-v2-dev1-task --profile dev1

    # Full-lifecycle churn: replay a mixed state-change/tag-change sequence per instance (see scenario_example.txt)
    python simulate_ec2_discovery_event.py --account-number 123456789012 --region us-east-1 \\
        --instance-ids-file ids.txt --scenario-file scenario_example.txt --step-delay-seconds 5 \\
        --concurrency 10 --invoke-lambda --function-name sns-subscription-v2-dev1-task --profile dev1

    # Soak: sustain a steady rate of tag-change events across a pool of instances for 10 minutes
    python simulate_ec2_discovery_event.py --account-number 123456789012 --region us-east-1 \\
        --instance-ids-file ids.txt --mode tag-change --tag-keys "Environment=stage" \\
        --rate-per-second 1 --duration-seconds 600 --invoke-lambda --function-name sns-subscription-v2-dev1-task --profile dev1

Every invocation (regardless of mode) is also appended as one row to
generated_events/run-summary-<run-id>.csv (timestamp, event type, instance id, Lambda status
code, Lambda duration) to make correlating against CloudWatch afterwards straightforward.
"""
import argparse
import csv
import json
import os
import random
import string
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "generated_events")


def _hex(n):
    return "".join(random.choices("0123456789abcdef", k=n))


def _fake_instance_id():
    return f"i-0{_hex(16)}"


def _fake_ami_id():
    return f"ami-0{_hex(16)}"


def _fake_reservation_id():
    return f"r-0{_hex(16)}"


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_run_instances_cloudtrail_detail(account_number, region, count, instance_type, ami_id, tags):
    """Build one CloudTrail record for a single RunInstances call that created `count` instances
    (matches how AWS emits ONE event per run-instances call, even with a count > 1)."""
    tag_items = [{"key": k, "value": v} for k, v in tags.items()]
    instance_items = []
    for _ in range(count):
        instance_items.append({
            "instanceId": _fake_instance_id(),
            "imageId": ami_id,
            "instanceState": {"code": 0, "name": "pending"},
            "instanceType": instance_type,
            "launchTime": _now_iso(),
            "placement": {"availabilityZone": f"{region}a", "tenancy": "default"},
            "amiLaunchIndex": 0,
            "tagSet": {"items": tag_items} if tag_items else {},
            "monitoring": {"state": "disabled"},
        })

    return {
        "eventVersion": "1.08",
        "userIdentity": {
            "type": "IAMUser",
            "principalId": _hex(21).upper(),
            "accountId": account_number,
            "userName": "simulated-user",
        },
        "eventTime": _now_iso(),
        "eventSource": "ec2.amazonaws.com",
        "eventName": "RunInstances",
        "awsRegion": region,
        "sourceIPAddress": "127.0.0.1",
        "userAgent": "simulate_ec2_discovery_event.py",
        "requestParameters": {
            "instancesSet": {"items": [{"imageId": ami_id, "minCount": count, "maxCount": count}]},
            "instanceType": instance_type,
        },
        "responseElements": {
            "requestId": str(uuid.uuid4()),
            "reservationId": _fake_reservation_id(),
            "instancesSet": {"items": instance_items},
        },
        "requestID": str(uuid.uuid4()),
        "eventID": str(uuid.uuid4()),
        "eventType": "AwsApiCall",
        "managementEvent": True,
        "recipientAccountId": account_number,
    }


def build_eventbridge_event(cloudtrail_detail, account_number, region):
    return {
        "version": "0",
        "id": str(uuid.uuid4()),
        "detail-type": "AWS API Call via CloudTrail",
        "source": "aws.ec2",
        "account": account_number,
        "time": _now_iso(),
        "region": region,
        "resources": [],
        "detail": cloudtrail_detail,
    }


def build_sns_envelope(eventbridge_event, account_number, region, topic_name):
    topic_arn = f"arn:aws:sns:{region}:{account_number}:{topic_name}"
    return {
        "Type": "Notification",
        "MessageId": str(uuid.uuid4()),
        "TopicArn": topic_arn,
        "Message": json.dumps(eventbridge_event),
        "Timestamp": _now_iso(),
    }


def build_lambda_event(sns_envelope):
    # SQS-trigger shape: queue_wrapper (mc_helpers/mc_queue_handler.py) requires a top-level
    # "Records" list and unwraps each record's own "body" before calling task(record, ...)
    return {
        "Records": [
            {
                "messageId": str(uuid.uuid4()),
                "receiptHandle": str(uuid.uuid4()),
                "body": json.dumps(sns_envelope),
            }
        ]
    }


# Matches EC2_INSTANCE_STATE in api/modules/services/sns_subsription/handler.py.
EC2_INSTANCE_STATE_DETAIL_TYPE = "EC2 Instance State-change Notification"

# Real EC2 state-transition order; "create"/"delete" simulate the two edges MontyCloud
# actually reacts to (handler.py's EC2_INSTANCE_STATE branch fires on every state).
ACTION_STATE_SEQUENCES = {
    "create": ["pending", "running"],
    "delete": ["shutting-down", "terminated"],
}


def build_instance_state_change_eventbridge_event(instance_id, account_number, region, state):
    """Build the native (non-CloudTrail) EventBridge event EC2 emits on a real state transition."""
    return {
        "version": "0",
        "id": str(uuid.uuid4()),
        "detail-type": EC2_INSTANCE_STATE_DETAIL_TYPE,
        "source": "aws.ec2",
        "account": account_number,
        "time": _now_iso(),
        "region": region,
        "resources": [f"arn:aws:ec2:{region}:{account_number}:instance/{instance_id}"],
        "detail": {"instance-id": instance_id, "state": state},
    }


def generate_state_change(args, instance_id, state):
    """Generate a single EC2 Instance State-change Notification event for one instance."""
    eventbridge_event = build_instance_state_change_eventbridge_event(
        instance_id, args.account_number, args.region, state,
    )
    sns_envelope = build_sns_envelope(eventbridge_event, args.account_number, args.region, args.topic_name)
    lambda_event = build_lambda_event(sns_envelope)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"{args.run_id}-{instance_id}-{state}.json")
    with open(out_path, "w") as f:
        json.dump(lambda_event, f, indent=2)

    print(f"Simulated 'EC2 Instance State-change Notification' ({state}) for {instance_id}")
    print(f"Saved Lambda-invoke-ready payload to {out_path}")
    return lambda_event


# Matches TAG_CHANGE_ON_RESOURCE in api/shared/constant/__init__.py.
TAG_CHANGE_ON_RESOURCE_DETAIL_TYPE = "Tag Change on Resource"


def build_instance_tag_change_eventbridge_event(instance_id, account_number, region, tags, changed_tag_keys):
    """Build the native (non-CloudTrail) 'Tag Change on Resource' event EC2 emits when an
    instance's tags are created/updated/deleted. Schema verified against
    instance_tag_changed_payload in api/domains/compute/compute_unit_tests/__init__.py."""
    return {
        "version": "0",
        "id": str(uuid.uuid4()),
        "detail-type": TAG_CHANGE_ON_RESOURCE_DETAIL_TYPE,
        "source": "aws.tag",
        "account": account_number,
        "time": _now_iso(),
        "region": region,
        "resources": [f"arn:aws:ec2:{region}:{account_number}:instance/{instance_id}"],
        "detail": {
            "changed-tag-keys": changed_tag_keys,
            "version": 1,
            "resource-type": "instance",
            "service": "ec2",
            "tags": tags,
        },
    }


def _fetch_existing_tags(args, instance_id):
    """Fetch the instance's current tags so the simulated event's 'tags' field matches AWS's real
    'Tag Change on Resource' contract, which always reports the FULL tag set after the change, not just
    the delta. Falls back to {} (old behavior) if the describe call fails, e.g. no AWS creds available."""
    try:
        import boto3  # imported lazily: only needed when fetching existing tags

        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        client = session.client("ec2")
        response = client.describe_tags(Filters=[{"Name": "resource-id", "Values": [instance_id]}])
        return {t["Key"]: t["Value"] for t in response.get("Tags", []) if not t["Key"].startswith("aws:")}
    except Exception as e:
        print(f"Warning: could not fetch existing tags for {instance_id} ({e}); "
              f"'tags' field will only contain --tag-keys, not matching AWS's real full-tag-set contract")
        return {}


def generate_tag_change(args, instance_id, new_tags):
    """Generate a single 'Tag Change on Resource' event for one instance. 'tags' carries the FULL current
    tag set (existing + new_tags merged), matching AWS's native contract; 'changed-tag-keys' is only the
    keys actually passed in new_tags (e.g. via --tag-keys)."""
    existing_tags = {} if args.skip_existing_tags_fetch else _fetch_existing_tags(args, instance_id)
    merged_tags = {**existing_tags, **new_tags}
    eventbridge_event = build_instance_tag_change_eventbridge_event(
        instance_id, args.account_number, args.region, merged_tags, list(new_tags),
    )
    sns_envelope = build_sns_envelope(eventbridge_event, args.account_number, args.region, args.topic_name)
    lambda_event = build_lambda_event(sns_envelope)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"{args.run_id}-{instance_id}-tagchange.json")
    with open(out_path, "w") as f:
        json.dump(lambda_event, f, indent=2)

    print(f"Simulated 'Tag Change on Resource' for {instance_id}: changed-tag-keys={list(new_tags)}, full tags={merged_tags}")
    print(f"Saved Lambda-invoke-ready payload to {out_path}")
    return lambda_event


def generate(args):
    tags = {"Name": f"{args.tag_prefix}-{args.run_id}"}
    cloudtrail_detail = build_run_instances_cloudtrail_detail(
        args.account_number, args.region, args.count, args.instance_type, args.ami_id, tags,
    )
    eventbridge_event = build_eventbridge_event(cloudtrail_detail, args.account_number, args.region)
    sns_envelope = build_sns_envelope(eventbridge_event, args.account_number, args.region, args.topic_name)
    lambda_event = build_lambda_event(sns_envelope)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"{args.run_id}.json")
    with open(out_path, "w") as f:
        json.dump(lambda_event, f, indent=2)

    instance_ids = [i["instanceId"] for i in cloudtrail_detail["responseElements"]["instancesSet"]["items"]]
    print(f"Simulated RunInstances event for {args.count} instance(s): {instance_ids}")
    print(f"Saved Lambda-invoke-ready payload to {out_path}")
    return lambda_event


_lambda_clients = {}
_lambda_clients_lock = threading.Lock()


def _get_lambda_client(args):
    """Cache one boto3 Lambda client per (profile, region): boto3 Sessions aren't thread-safe but
    clients are, and sharing one client also means one connection pool instead of one per call."""
    key = (args.profile, args.lambda_region or args.region)
    with _lambda_clients_lock:
        client = _lambda_clients.get(key)
        if client is None:
            import boto3
            from botocore.config import Config

            session = boto3.Session(profile_name=args.profile, region_name=key[1])
            pool_size = max(100, getattr(args, "concurrency", 0) or 0)
            client = session.client("lambda", config=Config(
                max_pool_connections=pool_size,
                retries={"mode": "standard", "max_attempts": 1},  # our own wrapper already retries on throttling
            ))
            _lambda_clients[key] = client
        return client


def invoke_lambda(args, lambda_event):
    client = _get_lambda_client(args)
    # Event = fire-and-forget (don't wait for the downstream Lambda to finish) for max dispatch throughput.
    invocation_type = "Event" if getattr(args, "async_invoke", False) else "RequestResponse"
    print(f"Invoking {args.function_name} in {args.lambda_region or args.region} ({invocation_type}) with the simulated payload...")
    start = time.perf_counter()
    response = client.invoke(
        FunctionName=args.function_name,
        InvocationType=invocation_type,
        Payload=json.dumps(lambda_event).encode("utf-8"),
    )
    duration_ms = round((time.perf_counter() - start) * 1000, 1)
    if invocation_type == "Event":
        # StatusCode 202 only means AWS accepted the invoke request, not that the function succeeded.
        print(f"Lambda accepted (async) status code: {response['StatusCode']} ({duration_ms} ms)")
    else:
        payload = response["Payload"].read().decode("utf-8")
        print(f"Lambda status code: {response['StatusCode']} ({duration_ms} ms)")
        print(f"Lambda response: {payload}")
    return response["StatusCode"], duration_ms


def invoke_lambda_with_retry(args, lambda_event, max_retries=5):
    """Wrap invoke_lambda with exponential backoff + jitter on Lambda invoke throttling, since account/
    reserved-concurrency limits for the target function are not known ahead of time."""
    from botocore.exceptions import ClientError  # imported lazily: only needed for --invoke-lambda

    attempt = 0
    while True:
        try:
            status_code, duration_ms = invoke_lambda(args, lambda_event)
            return status_code, duration_ms, attempt
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "TooManyRequestsException" or attempt >= max_retries:
                raise
            sleep_s = (2 ** attempt) * 0.5 + random.uniform(0, 0.5)
            print(f"Throttled invoking {args.function_name}, retrying in {sleep_s:.1f}s (attempt {attempt + 1}/{max_retries})...")
            time.sleep(sleep_s)
            attempt += 1


def _record_summary_row(args, event_type, instance_id, status_code, duration_ms, retries=0):
    """Append one row to generated_events/run-summary-<run-id>.csv for easy CloudWatch correlation."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    summary_path = os.path.join(OUTPUT_DIR, f"run-summary-{args.run_id}.csv")
    is_new = not os.path.exists(summary_path)
    with open(summary_path, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "event_type", "instance_id", "lambda_status_code", "lambda_duration_ms", "throttle_retries"])
        writer.writerow([_now_iso(), event_type, instance_id, status_code, duration_ms, retries])


def _dispatch(args, event_type, instance_id, lambda_event):
    """Optionally invoke the Lambda for one already-built/saved event, then record it in the run summary."""
    status_code = duration_ms = None
    retries = 0
    if args.invoke_lambda:
        status_code, duration_ms, retries = invoke_lambda_with_retry(args, lambda_event)
    _record_summary_row(args, event_type, instance_id, status_code, duration_ms, retries)


def _run_tasks(args, task_builders):
    """Run a flat list of zero-arg callables (each returns (event_type, instance_id, lambda_event) and
    has already saved its own payload file), respecting --concurrency and --ramp-seconds."""
    n = len(task_builders)
    concurrency = max(1, args.concurrency)
    ramp = args.ramp_seconds or 0
    delay_step = (ramp / n) if (ramp and n > 1) else 0

    def _run_one(index, builder):
        if delay_step:
            time.sleep(index * delay_step)
        event_type, instance_id, lambda_event = builder()
        _dispatch(args, event_type, instance_id, lambda_event)

    if concurrency <= 1 and not delay_step:
        for i, builder in enumerate(task_builders):
            _run_one(i, builder)
        return

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(_run_one, i, builder) for i, builder in enumerate(task_builders)]
        for future in futures:
            future.result()


def _parse_scenario_file(path):
    """Parse a scenario file into an ordered list of ("state-change", state) / ("tag-change", tags_dict) steps.
    One step per line: 'state-change:<state>' or 'tag-change:Key=Value[,Key2=Value2...]'; blank lines and
    lines starting with '#' are ignored."""
    steps = []
    with open(path) as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            kind, _, rest = line.partition(":")
            kind = kind.strip()
            if kind == "state-change":
                steps.append(("state-change", rest.strip()))
            elif kind == "tag-change":
                tags = {}
                for pair in rest.split(","):
                    key, _, value = pair.partition("=")
                    tags[key.strip()] = value.strip()
                steps.append(("tag-change", tags))
            else:
                raise ValueError(f"Unknown scenario step type {kind!r} in line: {line!r}")
    return steps


def _run_scenario_for_instance(args, instance_id, steps):
    """Replay an ordered scenario (state-change/tag-change steps) for one instance, honoring --step-delay-seconds
    between steps. Steps for a single instance always run in order, never in parallel with each other."""
    for i, (kind, payload) in enumerate(steps):
        if i > 0 and args.step_delay_seconds:
            time.sleep(args.step_delay_seconds)
        if kind == "state-change":
            lambda_event = generate_state_change(args, instance_id, payload)
            _dispatch(args, "InstanceStateChanged", instance_id, lambda_event)
        else:
            lambda_event = generate_tag_change(args, instance_id, payload)
            _dispatch(args, "InstanceTagChanged", instance_id, lambda_event)


def _run_soak(args, instance_ids):
    """Fire --mode events at a steady --rate-per-second, cycling through instance_ids, for --duration-seconds."""
    interval = 1.0 / args.rate_per_second
    concurrency = max(1, args.concurrency)
    executor = ThreadPoolExecutor(max_workers=concurrency) if concurrency > 1 else None
    futures = []
    count = 0
    end_time = time.monotonic() + args.duration_seconds
    try:
        while time.monotonic() < end_time:
            instance_id = instance_ids[count % len(instance_ids)]
            count += 1

            if args.mode == "tag-change":
                def _task(iid=instance_id, tags=args.tags):
                    lambda_event = generate_tag_change(args, iid, tags)
                    _dispatch(args, "InstanceTagChanged", iid, lambda_event)
            else:
                state = args.state or ACTION_STATE_SEQUENCES[args.action][-1]

                def _task(iid=instance_id, state=state):
                    lambda_event = generate_state_change(args, iid, state)
                    _dispatch(args, "InstanceStateChanged", iid, lambda_event)

            if executor:
                futures.append(executor.submit(_task))
            else:
                _task()
            time.sleep(interval)
    finally:
        if executor:
            for future in futures:
                future.result()
            executor.shutdown()
    print(f"Soak complete: {count} event(s) over {args.duration_seconds}s (~{args.rate_per_second}/s)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account-number", required=True, help="AWS account number (must already be onboarded in MontyCloud for a real Lambda invoke to resolve CustomerId)")
    parser.add_argument("--region", required=True, help="AWS region to simulate the instance(s) in")
    parser.add_argument("--mode", choices=["state-change", "cloudtrail", "tag-change"], default="state-change",
                        help="'state-change' (default) is what actually drives MontyCloud rediscovery; "
                             "'cloudtrail' reproduces the raw RunInstances CloudTrail event, which is filtered as noise; "
                             "'tag-change' simulates a 'Tag Change on Resource' event for an instance")
    parser.add_argument("--instance-id", action="append", default=None,
                         help="Required for --mode state-change/tag-change. One event is generated PER instance-id, so to "
                              "simulate N instances you need N distinct REAL instance ids already in the onboarded "
                              "account (the downstream consumer does a live describe_instances by this id — a "
                              "fabricated id, or reusing the same real id, will not create additional resources). "
                              "Repeat this flag or pass a comma-separated list; see also --instance-ids-file")
    parser.add_argument("--instance-ids-file", default=None,
                         help="Path to a file with one real instance id per line, as an alternative to repeating --instance-id")
    parser.add_argument("--action", choices=sorted(ACTION_STATE_SEQUENCES), default="create",
                         help="For --mode state-change: 'create' sends pending->running, 'delete' sends "
                              "shutting-down->terminated (default: create)")
    parser.add_argument("--state", choices=["pending", "running", "stopping", "stopped", "shutting-down", "terminated"],
                         default=None, help="For --mode state-change: send only this single state instead of --action's sequence")
    parser.add_argument("--count", type=int, default=1, help="[--mode cloudtrail only] Number of instances in the simulated RunInstances call (default: 1)")
    parser.add_argument("--instance-type", default="t3.micro")
    parser.add_argument("--ami-id", default=None, help="[--mode cloudtrail only] Defaults to a random fake AMI id if omitted")
    parser.add_argument("--tag-prefix", default="mc-sim")
    parser.add_argument("--tag-keys", default=None,
                         help="[--mode tag-change only, required] Comma-separated Key=Value pairs reported as the "
                              "CURRENT full tag set after the change, e.g. 'Name=demo,Environment=prod'. All keys "
                              "given here are reported in changed-tag-keys (add/update/remove-only scenarios are a follow-up).")
    parser.add_argument("--skip-existing-tags-fetch", action="store_true",
                         help="[--mode tag-change only] Skip fetching the instance's existing tags via EC2 "
                              "describe_tags before building the event; 'tags' will then only contain "
                              "--tag-keys instead of the full current tag set (old behavior, not matching "
                              "AWS's real contract). Useful if you don't have/need live AWS read access.")
    parser.add_argument("--topic-name", default="mc-account-notification-topic", help="Topic name used to build the fake TopicArn")
    parser.add_argument("--run-id", default=None, help="Reuse a specific run id instead of generating one")
    parser.add_argument("--invoke-lambda", action="store_true", help="Also invoke a real deployed sns_subsription Lambda with the generated payload")
    parser.add_argument("--function-name", default=None, help="Required with --invoke-lambda: target Lambda function name")
    parser.add_argument("--profile", default=None, help="AWS profile to use with --invoke-lambda")
    parser.add_argument("--lambda-region", default=None, help="Region of the Lambda function, if different from --region")
    parser.add_argument("--async-invoke", action="store_true",
                         help="Invoke with InvocationType=Event (fire-and-forget) instead of RequestResponse, for "
                              "max dispatch throughput; per-call status/duration then reflect submission only, "
                              "not downstream Lambda success/duration")
    parser.add_argument("--concurrency", type=int, default=1,
                         help="Max parallel Lambda dispatches (default: 1 = sequential, same as before). Applies to "
                              "the state-change/tag-change instance loops, --scenario-file (across instances), and "
                              "soak mode")
    parser.add_argument("--ramp-seconds", type=float, default=0,
                         help="Spread dispatch start times evenly across this many seconds instead of firing all at "
                              "once (only meaningful with --concurrency > 1; ignored by soak mode). Default: 0")
    parser.add_argument("--scenario-file", default=None,
                         help="Path to a scenario file: one step per line, 'state-change:<state>' or "
                              "'tag-change:Key=Value[,Key2=Value2...]'. Each instance id (from --instance-id/"
                              "--instance-ids-file) replays the full ordered sequence. Ignores --mode/--action/"
                              "--state/--tag-keys (steps define their own types/payloads); mutually exclusive with "
                              "--rate-per-second/--duration-seconds")
    parser.add_argument("--step-delay-seconds", type=float, default=0,
                         help="Delay between steps within one instance's --scenario-file sequence (default: 0)")
    parser.add_argument("--rate-per-second", type=float, default=None,
                         help="Soak mode: fire events at this steady rate, cycling through instance ids, for "
                              "--duration-seconds. Requires --duration-seconds. Uses --mode (state-change/tag-change) "
                              "to pick the event type; mutually exclusive with --scenario-file")
    parser.add_argument("--duration-seconds", type=float, default=None,
                         help="Soak mode: total duration to sustain --rate-per-second for. Requires --rate-per-second")

    args = parser.parse_args()
    args.ami_id = args.ami_id or _fake_ami_id()
    args.run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + "".join(random.choices(string.ascii_lowercase, k=4))

    if args.invoke_lambda and not args.function_name:
        parser.error("--invoke-lambda requires --function-name")

    if args.mode == "cloudtrail":
        lambda_event = generate(args)
        if args.invoke_lambda:
            invoke_lambda(args, lambda_event)
        return

    instance_ids = []
    for raw in args.instance_id or []:
        instance_ids.extend(part.strip() for part in raw.split(",") if part.strip())
    if args.instance_ids_file:
        with open(args.instance_ids_file) as f:
            instance_ids.extend(line.strip() for line in f if line.strip())
    if not instance_ids:
        parser.error(f"--mode {args.mode} requires --instance-id (repeatable/comma-separated) or --instance-ids-file, "
                     "using real instance ids from the onboarded account")

    if args.scenario_file and (args.rate_per_second or args.duration_seconds):
        parser.error("--scenario-file and --rate-per-second/--duration-seconds are mutually exclusive")
    if bool(args.rate_per_second) != bool(args.duration_seconds):
        parser.error("--rate-per-second and --duration-seconds must be used together")

    if args.scenario_file:
        steps = _parse_scenario_file(args.scenario_file)
        print(f"Running a {len(steps)}-step scenario x {len(instance_ids)} instance(s), concurrency={args.concurrency}")
        if args.concurrency <= 1:
            for instance_id in instance_ids:
                _run_scenario_for_instance(args, instance_id, steps)
        else:
            with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                futures = [executor.submit(_run_scenario_for_instance, args, instance_id, steps) for instance_id in instance_ids]
                for future in futures:
                    future.result()
        return

    if args.mode == "tag-change":
        if not args.tag_keys:
            parser.error("--mode tag-change requires --tag-keys, e.g. --tag-keys 'Name=demo,Environment=prod'")
        tags = {}
        for pair in args.tag_keys.split(","):
            key, _, value = pair.partition("=")
            tags[key.strip()] = value.strip()
        args.tags = tags

    if args.rate_per_second:
        print(f"Soaking {args.mode} events at {args.rate_per_second}/s for {args.duration_seconds}s across "
              f"{len(instance_ids)} instance(s), concurrency={args.concurrency}")
        _run_soak(args, instance_ids)
        return

    if args.mode == "tag-change":
        print(f"Simulating 1 tag-change event x {len(instance_ids)} instance(s), concurrency={args.concurrency}")
        task_builders = [
            (lambda iid=instance_id: ("InstanceTagChanged", iid, generate_tag_change(args, iid, args.tags)))
            for instance_id in instance_ids
        ]
        _run_tasks(args, task_builders)
        return

    states = [args.state] if args.state else ACTION_STATE_SEQUENCES[args.action]
    print(f"Simulating {len(states)} state(s) x {len(instance_ids)} instance(s) = "
          f"{len(states) * len(instance_ids)} event(s), concurrency={args.concurrency}")
    task_builders = [
        (lambda iid=instance_id, st=state: ("InstanceStateChanged", iid, generate_state_change(args, iid, st)))
        for instance_id in instance_ids
        for state in states
    ]
    _run_tasks(args, task_builders)


if __name__ == "__main__":
    sys.exit(main())
