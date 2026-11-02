#!/usr/bin/env python3
"""Simulate a 'Tag Change on Resource' event for instances spread across MULTIPLE AWS accounts in
one run — simulate_ec2_discovery_event.py only takes a single --account-number/--region per
invocation, so this wraps it for the common "N instances across M customer accounts" case.

Reuses simulate_ec2_discovery_event.py's own event-building/dispatch logic (same tag-merge,
same Lambda invoke, same run-summary CSV) — nothing is duplicated, just grouped by account+region.

Mapping file format (CSV, header required):
    account_number,region,instance_id
    111111111111,us-east-1,i-aaaaaaaaaaaaaaaaa
    111111111111,us-east-1,i-bbbbbbbbbbbbbbbbb
    222222222222,us-west-2,i-ccccccccccccccccc
    ...

Usage:
    python simulate_multi_account_tag_change.py --mapping-file accounts_instances.csv \\
        --tag-keys "perftesttag=loadtest-26oct" \\
        --invoke-lambda --function-name sns-subscription-v2-stg1-task \\
        --profile stg1 --lambda-region us-east-2

    # Fire 10 SEPARATE 'Tag Change on Resource' events per instance (one per tag, changed-tag-keys
    # scoped to just that tag each time, 'tags' accumulating progressively) instead of one event
    # carrying all 10 tags at once:
    python simulate_multi_account_tag_change.py --mapping-file accounts_instances.csv \\
        --tag-keys "k1=v1,k2=v2,k3=v3,k4=v4,k5=v5,k6=v6,k7=v7,k8=v8,k9=v9,k10=v10" \\
        --separate-events \\
        --invoke-lambda --function-name sns-subscription-v2-stg1-task \\
        --profile stg1 --lambda-region us-east-2

The MontyCloud Lambda itself is the same for every customer account (it resolves CustomerId from
AccountNumber internally), so --function-name/--profile/--lambda-region stay constant across all
10 accounts; only --account-number/--region change per instance, which this script handles.
"""

import argparse
import csv
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import simulate_ec2_discovery_event as sim

_summary_lock = threading.Lock()


class RateLimiter:
    """Simple shared token-bucket-ish limiter: caps invokes/sec across all threads via a single
    monotonically-advancing 'next allowed slot' timestamp, protected by a lock."""

    def __init__(self, rate_per_second):
        self.interval = 1.0 / rate_per_second if rate_per_second else 0
        self._lock = threading.Lock()
        self._next_slot = time.monotonic()

    def wait(self):
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.interval
        sleep_s = slot - time.monotonic()
        if sleep_s > 0:
            time.sleep(sleep_s)


def _load_mapping(path):
    """Group instance ids by (account_number, region)."""
    groups = defaultdict(list)
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        required = {"account_number", "region", "instance_id"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"Mapping file must have columns {required}, found {reader.fieldnames}")
        for row in reader:
            groups[(row["account_number"].strip(), row["region"].strip())].append(row["instance_id"].strip())
    return groups


def _dispatch_threadsafe(group_args, event_type, instance_id, lambda_event, rate_limiter=None):
    """Same as sim._dispatch, but serializes the shared run-summary CSV append across threads
    (the Lambda invoke itself, the slow/network part, still runs fully in parallel)."""
    status_code = duration_ms = None
    retries = 0
    if group_args.invoke_lambda:
        if rate_limiter:
            rate_limiter.wait()
        status_code, duration_ms, retries = sim.invoke_lambda_with_retry(group_args, lambda_event)
    with _summary_lock:
        sim._record_summary_row(group_args, event_type, instance_id, status_code, duration_ms, retries)


def _build_separate_events(group_args, instance_id, tags):
    """Precompute one 'Tag Change on Resource' payload per tag in `tags` (no network calls besides the
    one upfront describe_tags), accumulating the full tag set progressively — matching AWS's real
    contract: 'tags' is always the full current state, 'changed-tag-keys' is only the key(s) that
    changed in that specific CreateTags call. Returns a list of (event_type, instance_id, lambda_event),
    ready to be dispatched in any order/concurrency."""
    existing_tags = {} if group_args.skip_existing_tags_fetch else sim._fetch_existing_tags(group_args, instance_id)
    accumulated = dict(existing_tags)
    events = []
    for key, value in tags.items():
        accumulated[key] = value
        eventbridge_event = sim.build_instance_tag_change_eventbridge_event(
            instance_id, group_args.account_number, group_args.region, dict(accumulated), [key],
        )
        sns_envelope = sim.build_sns_envelope(eventbridge_event, group_args.account_number, group_args.region, group_args.topic_name)
        lambda_event = sim.build_lambda_event(sns_envelope)
        print(f"Built 'Tag Change on Resource' for {instance_id}: changed-tag-keys=['{key}'], full tags={accumulated}")
        events.append(("InstanceTagChanged", instance_id, lambda_event))
    return events


def _build_events_for_instance(group_args, instance_id, tags, separate_events):
    """Precompute all event(s) for ONE instance (sequential internally — tag accumulation order
    matters within an instance — but produces plain data, so the resulting events can be dispatched
    by any thread/order once flattened across instances)."""
    if separate_events:
        return _build_separate_events(group_args, instance_id, tags)
    lambda_event = sim.generate_tag_change(group_args, instance_id, tags)
    return [("InstanceTagChanged", instance_id, lambda_event)]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mapping-file", required=True, help="CSV with columns: account_number,region,instance_id")
    parser.add_argument("--tag-keys", required=True, help="Comma-separated Key=Value pairs, e.g. 'Name=demo,Env=prod'")
    parser.add_argument("--separate-events", action="store_true",
                         help="Fire ONE 'Tag Change on Resource' event PER tag in --tag-keys (changed-tag-keys "
                              "= just that tag each time, 'tags' accumulates progressively), instead of one "
                              "event carrying all tags at once")
    parser.add_argument("--skip-existing-tags-fetch", action="store_true",
                         help="Skip fetching each instance's existing tags via EC2 describe_tags before building the event")
    parser.add_argument("--topic-name", default="mc-account-notification-topic")
    parser.add_argument("--invoke-lambda", action="store_true")
    parser.add_argument("--function-name", default=None, help="Required with --invoke-lambda")
    parser.add_argument("--profile", default=None, help="AWS profile used for --invoke-lambda and existing-tags fetch")
    parser.add_argument("--lambda-region", default=None, help="Region of the Lambda, if different from each instance's region")
    parser.add_argument("--concurrency", type=int, default=1,
                         help="Max concurrent event DISPATCHES (Lambda invokes), not instances — safe to set much "
                              "higher than the instance count (e.g. 200-400) now that build (tag accumulation) and "
                              "dispatch are two separate phases. Default: 1 = sequential")
    parser.add_argument("--async-invoke", action="store_true",
                         help="Invoke with InvocationType=Event (fire-and-forget) instead of RequestResponse, for "
                              "max throughput; per-event status/duration then reflect submission only")
    parser.add_argument("--max-invokes-per-second", type=float, default=None,
                         help="Optional global rate cap across all threads, as a safety valve when the target "
                              "Lambda's account/reserved concurrency limit is unknown. Default: unlimited")
    args = parser.parse_args()

    if args.invoke_lambda and not args.function_name:
        parser.error("--invoke-lambda requires --function-name")

    tags = {}
    for pair in args.tag_keys.split(","):
        key, _, value = pair.partition("=")
        tags[key.strip()] = value.strip()

    groups = _load_mapping(args.mapping_file)
    total_instances = sum(len(ids) for ids in groups.values())
    print(f"Loaded {total_instances} instance(s) across {len(groups)} account/region group(s) from {args.mapping_file}")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-multiacct"

    build_tasks = []
    for (account_number, region), instance_ids in groups.items():
        group_args = argparse.Namespace(
            account_number=account_number,
            region=region,
            profile=args.profile,
            topic_name=args.topic_name,
            run_id=run_id,
            skip_existing_tags_fetch=args.skip_existing_tags_fetch,
            invoke_lambda=args.invoke_lambda,
            function_name=args.function_name,
            lambda_region=args.lambda_region,
            async_invoke=args.async_invoke,
            concurrency=args.concurrency,  # sizes the shared Lambda client's connection pool in sim._get_lambda_client
        )
        for instance_id in instance_ids:
            build_tasks.append((group_args, instance_id))

    concurrency = max(1, args.concurrency)

    # Phase 1: build every instance's event payload(s) (tag accumulation + one describe_tags call each) —
    # this is I/O but NOT Lambda invokes, so it can run at the same concurrency without touching the Lambda yet.
    print(f"Building events for {len(build_tasks)} instance(s) with concurrency={concurrency}"
          f"{' (separate-events mode)' if args.separate_events else ''}")
    flat_events = []  # list of (group_args, event_type, instance_id, lambda_event)
    if concurrency <= 1:
        for group_args, instance_id in build_tasks:
            for event_type, iid, lambda_event in _build_events_for_instance(group_args, instance_id, tags, args.separate_events):
                flat_events.append((group_args, event_type, iid, lambda_event))
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(_build_events_for_instance, group_args, instance_id, tags, args.separate_events): (group_args, instance_id)
                for group_args, instance_id in build_tasks
            }
            for future in as_completed(futures):
                group_args, instance_id = futures[future]
                try:
                    for event_type, iid, lambda_event in future.result():
                        flat_events.append((group_args, event_type, iid, lambda_event))
                except Exception as e:
                    print(f"FAILED building event(s) for {instance_id}: {e}")

    # Phase 2: dispatch ALL events (across every instance) through one flat pool — this is what was previously
    # bottlenecked by firing each instance's tags strictly sequentially inside a per-instance thread.
    print(f"Dispatching {len(flat_events)} event(s) with concurrency={concurrency}"
          f"{', rate-limited to ' + str(args.max_invokes_per_second) + '/s' if args.max_invokes_per_second else ''}")
    rate_limiter = RateLimiter(args.max_invokes_per_second) if args.max_invokes_per_second else None

    if concurrency <= 1:
        for group_args, event_type, instance_id, lambda_event in flat_events:
            _dispatch_threadsafe(group_args, event_type, instance_id, lambda_event, rate_limiter)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(_dispatch_threadsafe, group_args, event_type, instance_id, lambda_event, rate_limiter): instance_id
                for group_args, event_type, instance_id, lambda_event in flat_events
            }
            for future in as_completed(futures):
                instance_id = futures[future]
                try:
                    future.result()
                except Exception as e:
                    print(f"FAILED dispatching event for {instance_id}: {e}")

    print(f"\nDone. See generated_events/run-summary-{run_id}.csv for the full dispatch log.")


if __name__ == "__main__":
    sys.exit(main())
