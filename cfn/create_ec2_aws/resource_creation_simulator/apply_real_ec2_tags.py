#!/usr/bin/env python3
"""Apply REAL tags to real EC2 instances via ec2:CreateTags — use this (not the event simulator)
when you need the tag change to actually reflect in MontyCloud's UI, since Instance.Tags is
refreshed from a live AWS describe/tags call, not from the simulated event payload (see
simulate_ec2_discovery_event.py's docstring for that distinction).

Requires AWS credentials for the CUSTOMER account where the instances actually live (not the
MontyCloud control-plane profile used for --invoke-lambda in simulate_ec2_discovery_event.py).

Tagging N instances with the SAME tag set is done in batches via a single ec2:CreateTags call per
batch (AWS allows up to 1000 resources per call); use --batch-size to control how many instances
go in one API call. Use --unique-suffix to make each instance's tag value distinct (e.g. for
tracking which instance got which value in a load test), which forces one CreateTags call per
instance instead of batching.

Usage:
    # Same tag(s) on all instances, batched (fast, few API calls)
    python apply_real_ec2_tags.py --region us-east-1 --profile customer-account-profile \\
        --instance-ids-file ids.txt --tags "perftesttag=loadtest-26oct,Environment=stage"

    # Distinct tag value per instance (one API call per instance)
    python apply_real_ec2_tags.py --region us-east-1 --profile customer-account-profile \\
        --instance-ids-file ids.txt --tags "perftesttag=loadtest" --unique-suffix

    # Dry run: show what would be tagged without calling AWS
    python apply_real_ec2_tags.py --region us-east-1 --profile customer-account-profile \\
        --instance-ids-file ids.txt --tags "perftesttag=loadtest" --dry-run
"""
import argparse
import sys
import time


def _parse_tags(tags_arg):
    tags = {}
    for pair in tags_arg.split(","):
        key, _, value = pair.partition("=")
        tags[key.strip()] = value.strip()
    return tags


def _chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", required=True, help="AWS region the instances are in")
    parser.add_argument("--profile", default=None, help="AWS profile with ec2:CreateTags access on the customer account")
    parser.add_argument("--instance-id", action="append", default=None, help="Repeatable, or comma-separated")
    parser.add_argument("--instance-ids-file", default=None, help="Path to a file with one instance id per line")
    parser.add_argument("--tags", required=True, help="Comma-separated Key=Value pairs to apply, e.g. 'Name=demo,Env=stage'")
    parser.add_argument("--unique-suffix", action="store_true",
                         help="Append '-<index>' to every tag value so each instance gets a distinct value "
                              "(useful for tracking which instance got tagged in a load test). Forces one "
                              "ec2:CreateTags call per instance instead of batching.")
    parser.add_argument("--batch-size", type=int, default=20,
                         help="Instances per ec2:CreateTags call when --unique-suffix is not set (max 1000 per AWS). Default: 20")
    parser.add_argument("--delay-seconds", type=float, default=0.5, help="Delay between API calls, to stay under throttling limits")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be tagged without calling AWS")

    args = parser.parse_args()

    instance_ids = []
    for raw in args.instance_id or []:
        instance_ids.extend(part.strip() for part in raw.split(",") if part.strip())
    if args.instance_ids_file:
        with open(args.instance_ids_file) as f:
            instance_ids.extend(line.strip() for line in f if line.strip())
    if not instance_ids:
        parser.error("Provide --instance-id (repeatable/comma-separated) or --instance-ids-file")

    base_tags = _parse_tags(args.tags)

    if args.dry_run:
        print(f"[DRY RUN] Would tag {len(instance_ids)} instance(s) in {args.region}")
        for i, instance_id in enumerate(instance_ids):
            tags = {k: f"{v}-{i}" for k, v in base_tags.items()} if args.unique_suffix else base_tags
            print(f"  {instance_id}: {tags}")
        return

    import boto3  # imported lazily: only needed for the real AWS call

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("ec2")

    tagged_count = 0
    failed = []

    if args.unique_suffix:
        for i, instance_id in enumerate(instance_ids):
            tags = {k: f"{v}-{i}" for k, v in base_tags.items()}
            tag_set = [{"Key": k, "Value": v} for k, v in tags.items()]
            try:
                client.create_tags(Resources=[instance_id], Tags=tag_set)
                tagged_count += 1
                print(f"Tagged {instance_id} with {tags}")
            except Exception as e:
                print(f"FAILED to tag {instance_id}: {e}")
                failed.append(instance_id)
            time.sleep(args.delay_seconds)
    else:
        tag_set = [{"Key": k, "Value": v} for k, v in base_tags.items()]
        for batch in _chunk(instance_ids, args.batch_size):
            try:
                client.create_tags(Resources=batch, Tags=tag_set)
                tagged_count += len(batch)
                print(f"Tagged batch of {len(batch)} instance(s): {batch}")
            except Exception as e:
                print(f"FAILED to tag batch {batch}: {e}")
                failed.extend(batch)
            time.sleep(args.delay_seconds)

    print(f"\nDone: {tagged_count}/{len(instance_ids)} tagged successfully, {len(failed)} failed")
    if failed:
        print(f"Failed instance ids: {failed}")
        sys.exit(1)


if __name__ == "__main__":
    main()
