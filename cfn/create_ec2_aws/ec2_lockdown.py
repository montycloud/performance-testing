#!/usr/bin/env python3
"""
ec2_lockdown.py

Creates one OR MORE locked-down, free-tier EC2 instances (per --count) in each
AWS account listed in accounts.txt, stops them immediately, and can
tag/stop/terminate them later.

accounts.txt format (CSV, header row required):
    accountNumber,region_to_create_ec2,profile_in_aws
    111111111111,us-east-1,dev-account
    222222222222,ap-south-1,prod-account

Credentials come from ~/.aws/credentials / ~/.aws/config via the profile name.
Before doing anything in an account, the script checks that the profile's
credentials really belong to that account number, and skips the row if not.

What "locked down" means:
  - No key pair (no PEM file)               -> nobody can SSH in
  - No public IP                            -> not reachable from the internet
  - Security group with NO inbound and NO outbound rules
  - No IAM instance profile                 -> no SSM Session Manager access
  - Instance metadata service disabled
  - EC2 Serial Console access disabled for the account/region
  - Smallest root disk (8 GiB gp3, encrypted, deleted on terminate)
  - Stopped as soon as it reaches "running"

Usage:
  python ec2_lockdown.py create                                  # create + stop (1 per account), all accounts in parallel
  python ec2_lockdown.py create --count 10                       # create + stop (10 per account)
  python ec2_lockdown.py create --ramp-size 5 --ramp-delay 60     # 5 accounts in parallel per batch, 60s between batches
  python ec2_lockdown.py status                                  # show instances
  python ec2_lockdown.py status --show-tags                      # show instances + their current tags
  python ec2_lockdown.py stop                                    # stop any running ones
  python ec2_lockdown.py terminate                               # terminate + delete SGs (asks to confirm)
  python ec2_lockdown.py terminate --yes                         # no confirmation prompt
  python ec2_lockdown.py terminate --only ACCT --instance-id ID  # terminate one specific instance
  python ec2_lockdown.py tag --add-tag Env=perf --remove-tag Old  # add/remove tags on all instances
  python ec2_lockdown.py tag --add-tag Env=perf --ramp-size 2 --ramp-delay 60  # ramp-up in batches

Options:
  --accounts FILE         accounts file (default: accounts.txt)
  --manifest FILE         created-instance manifest (default: instances.csv)
  --instance-type TYPE    force a type, e.g. t3.micro (default: auto-detect free tier)
  --count N               instances to create per account, action=create (default: 1)
  --only ACCT [ACCT ...]  run only for these account numbers
  --show-tags             action=status: also print each instance's tags
  --instance-id ID [ID ...] terminate exactly these instance ids (requires a single --only account)
  --region REGION         override the region for --instance-id (defaults to the accounts.txt row)
  --add-tag KEY=VALUE     tag to add (repeatable), action=tag
  --remove-tag KEY        tag key to remove (repeatable), action=tag
  --ramp-size N           apply `tag` action to N accounts at a time (default: all at once)
  --ramp-delay SECONDS    seconds to wait between ramp-up batches (default: 30)
  --dry-run               ask AWS to validate the calls without creating anything

Requires: Python 3.8+, boto3  (pip install boto3)
"""

import argparse
import csv
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound

TAG_KEY = "Purpose"
TAG_VALUE = "locked-placeholder"
SG_NAME = f"{TAG_VALUE}-no-traffic"
PREFERRED_TYPES = ["t3.micro", "t2.micro", "t4g.micro", "t3.small", "t4g.small"]
AMI_PARAM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-{arch}"

ALL_LIVE_STATES = ["pending", "running", "stopping", "stopped", "shutting-down"]

MANIFEST_FIELDS = ["account", "region", "instance_id", "sg_id", "created_at"]

# Actions whose accounts.txt rows are independent (own session/client) — safe to parallelize.
PARALLEL_ACTIONS = {"create", "tag"}


class AccountError(Exception):
    """A problem that should skip this account but not stop the whole run."""


# -----------------------------------------------------------------------------
# instances.csv manifest — the source of truth for instance ids, independent
# of EC2 tags (which the tag-change perf test will itself be adding/removing).
# -----------------------------------------------------------------------------
def read_manifest(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_manifest(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        w.writerows(rows)


def upsert_manifest(path, account, region, instance_id, sg_id):
    rows = [r for r in read_manifest(path)
            if not (r["account"] == account and r["instance_id"] == instance_id)]
    rows.append({
        "account": account,
        "region": region,
        "instance_id": instance_id,
        "sg_id": sg_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    write_manifest(path, rows)


def remove_from_manifest(path, account, instance_ids):
    ids = set(instance_ids)
    rows = [r for r in read_manifest(path)
            if not (r["account"] == account and r["instance_id"] in ids)]
    write_manifest(path, rows)


def manifest_instance_ids(path, account):
    return [r["instance_id"] for r in read_manifest(path) if r["account"] == account]


# -----------------------------------------------------------------------------
# accounts.txt
# -----------------------------------------------------------------------------
def read_accounts(path):
    rows = []
    with open(path, newline="") as f:
        lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    reader = csv.reader(lines)
    header = next(reader, None)
    if header is None:
        sys.exit(f"{path} is empty")
    for lineno, row in enumerate(reader, start=2):
        row = [c.strip() for c in row]
        if len(row) < 3 or not all(row[:3]):
            print(f"  ! {path} line {lineno}: expected accountNumber,region,profile — skipped")
            continue
        acct, region, profile = row[:3]
        acct = acct.zfill(12) if acct.isdigit() else acct
        rows.append({"account": acct, "region": region, "profile": profile})
    if not rows:
        sys.exit(f"No accounts found in {path}")
    return rows


# -----------------------------------------------------------------------------
# Account context
# -----------------------------------------------------------------------------
class Account:
    def __init__(self, account, region, profile, dry_run=False, manifest_path="instances.csv"):
        self.account = account
        self.region = region
        self.profile = profile
        self.dry_run = dry_run
        self.manifest_path = manifest_path
        try:
            self.session = boto3.Session(profile_name=profile, region_name=region)
        except ProfileNotFound:
            raise AccountError(f"profile '{profile}' not found in ~/.aws")
        self.ec2 = self.session.client("ec2")

    def log(self, msg):
        print(f"[{self.account} {self.region}] {msg}", flush=True)

    def verify_identity(self):
        try:
            actual = self.session.client("sts").get_caller_identity()["Account"]
        except NoCredentialsError:
            raise AccountError(f"profile '{self.profile}' has no usable credentials")
        except ClientError as e:
            raise AccountError(f"cannot authenticate with profile '{self.profile}': {e}")
        if actual != self.account:
            raise AccountError(
                f"profile '{self.profile}' belongs to account {actual}, "
                f"not {self.account} — fix accounts.txt"
            )
        self.log(f"authenticated with profile '{self.profile}'")

    # --- lookups -------------------------------------------------------------
    def our_instances(self, states):
        ids = []
        pager = self.ec2.get_paginator("describe_instances")
        for page in pager.paginate(Filters=[
            {"Name": f"tag:{TAG_KEY}", "Values": [TAG_VALUE]},
            {"Name": "instance-state-name", "Values": states},
        ]):
            for res in page["Reservations"]:
                ids.extend(i["InstanceId"] for i in res["Instances"])
        return ids

    def pick_instance_type(self, forced=None):
        if forced:
            return forced
        eligible = set()
        try:
            pager = self.ec2.get_paginator("describe_instance_types")
            for page in pager.paginate(
                Filters=[{"Name": "free-tier-eligible", "Values": ["true"]}]
            ):
                eligible.update(t["InstanceType"] for t in page["InstanceTypes"])
        except ClientError as e:
            raise AccountError(f"cannot list free-tier instance types: {e}")
        if not eligible:
            raise AccountError("no free-tier-eligible instance types in this account/region")
        for t in PREFERRED_TYPES:
            if t in eligible:
                return t
        return sorted(eligible)[0]

    def arch_for(self, itype):
        info = self.ec2.describe_instance_types(InstanceTypes=[itype])["InstanceTypes"][0]
        archs = info["ProcessorInfo"]["SupportedArchitectures"]
        return "arm64" if "arm64" in archs else "x86_64"

    def default_vpc(self):
        vpcs = self.ec2.describe_vpcs(
            Filters=[{"Name": "is-default", "Values": ["true"]}]
        )["Vpcs"]
        if not vpcs:
            raise AccountError(
                "no default VPC in this region — create one with "
                f"'aws ec2 create-default-vpc --region {self.region} --profile {self.profile}'"
            )
        return vpcs[0]["VpcId"]

    def subnet_for(self, vpc_id, itype):
        offerings = self.ec2.describe_instance_type_offerings(
            LocationType="availability-zone",
            Filters=[{"Name": "instance-type", "Values": [itype]}],
        )["InstanceTypeOfferings"]
        azs = [o["Location"] for o in offerings]
        if not azs:
            raise AccountError(f"{itype} is not offered in any AZ of {self.region}")
        subnets = self.ec2.describe_subnets(Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "availability-zone", "Values": azs},
        ])["Subnets"]
        if not subnets:
            raise AccountError(f"no subnet in the default VPC supports {itype}")
        return subnets[0]["SubnetId"]

    def ensure_sg(self, vpc_id):
        found = self.ec2.describe_security_groups(Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": [SG_NAME]},
        ])["SecurityGroups"]
        if found:
            sg_id = found[0]["GroupId"]
        elif self.dry_run:
            # Don't create a real SG for a dry run — borrow the VPC's default SG id just
            # to give run_instances(DryRun=True) a syntactically valid group to validate.
            default_sg = self.ec2.describe_security_groups(Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "group-name", "Values": ["default"]},
            ])["SecurityGroups"]
            self.log("dry run — skipping security group creation")
            return default_sg[0]["GroupId"]
        else:
            sg_id = self.ec2.create_security_group(
                VpcId=vpc_id,
                GroupName=SG_NAME,
                Description="No inbound and no outbound traffic",
                TagSpecifications=[{
                    "ResourceType": "security-group",
                    "Tags": [{"Key": TAG_KEY, "Value": TAG_VALUE}],
                }],
            )["GroupId"]
            self.log(f"created security group {sg_id}")
        # Make sure it has no rules at all, even if someone edited it.
        sg = self.ec2.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
        if sg.get("IpPermissions"):
            self.ec2.revoke_security_group_ingress(GroupId=sg_id, IpPermissions=sg["IpPermissions"])
        if sg.get("IpPermissionsEgress"):
            self.ec2.revoke_security_group_egress(GroupId=sg_id, IpPermissions=sg["IpPermissionsEgress"])
        return sg_id

    def instance_ids(self):
        """Union of manifest-recorded and tag-discovered ids — tolerant of tag mutation."""
        return list(set(self.our_instances(ALL_LIVE_STATES)) |
                     set(manifest_instance_ids(self.manifest_path, self.account)))

    # --- actions -------------------------------------------------------------
    def create(self, forced_type=None, count=1):
        existing = self.our_instances(ALL_LIVE_STATES)
        if len(existing) >= count:
            self.log(f"already has {len(existing)} instance(s) >= requested {count} — not creating more")
            for inst in self.ec2.describe_instances(InstanceIds=existing)["Reservations"]:
                for i in inst["Instances"]:
                    sgs = [g["GroupId"] for g in i.get("SecurityGroups", [])]
                    upsert_manifest(self.manifest_path, self.account, self.region,
                                     i["InstanceId"], sgs[0] if sgs else "")
            running = self.our_instances(["pending", "running"])
            if running:
                self.log(f"stopping {', '.join(running)}")
                self.ec2.stop_instances(InstanceIds=running)
                self.ec2.get_waiter("instance_stopped").wait(InstanceIds=running)
            return
        to_create = count - len(existing)

        try:
            self.ec2.disable_serial_console_access()
        except Exception as e:  # not permitted / unsupported — not critical
            self.log(f"note: could not disable serial console access ({type(e).__name__})")

        itype = self.pick_instance_type(forced_type)
        arch = self.arch_for(itype)
        vpc_id = self.default_vpc()
        subnet_id = self.subnet_for(vpc_id, itype)
        sg_id = self.ensure_sg(vpc_id)
        self.log(f"launching {to_create}x {itype} ({arch}) in {subnet_id}")

        tags = [{"Key": TAG_KEY, "Value": TAG_VALUE}, {"Key": "Name", "Value": TAG_VALUE}]
        try:
            resp = self.ec2.run_instances(
                DryRun=self.dry_run,
                ImageId="resolve:ssm:" + AMI_PARAM.format(arch=arch),
                InstanceType=itype,
                MinCount=to_create,
                MaxCount=to_create,
                NetworkInterfaces=[{
                    "DeviceIndex": 0,
                    "SubnetId": subnet_id,
                    "Groups": [sg_id],
                    "AssociatePublicIpAddress": False,
                    "DeleteOnTermination": True,
                }],
                MetadataOptions={"HttpEndpoint": "disabled"},
                BlockDeviceMappings=[{
                    "DeviceName": "/dev/xvda",
                    "Ebs": {
                        "VolumeSize": 8, "VolumeType": "gp3",
                        "DeleteOnTermination": True, "Encrypted": True,
                    },
                }],
                InstanceInitiatedShutdownBehavior="stop",
                TagSpecifications=[
                    {"ResourceType": "instance", "Tags": tags},
                    {"ResourceType": "volume", "Tags": [{"Key": TAG_KEY, "Value": TAG_VALUE}]},
                ],
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "DryRunOperation":
                self.log("dry run OK — the launch would have succeeded")
                return
            raise

        iids = [i["InstanceId"] for i in resp["Instances"]]
        self.log(f"created {', '.join(iids)}, waiting for them to be running")
        self.ec2.get_waiter("instance_running").wait(InstanceIds=iids)

        # Safety check: every instance must be in ONLY the no-traffic security group.
        insts = [i for res in self.ec2.describe_instances(InstanceIds=iids)["Reservations"]
                 for i in res["Instances"]]
        for inst in insts:
            attached = sorted(g["GroupId"] for g in inst.get("SecurityGroups", []))
            if attached != [sg_id]:
                self.log(f"fixing security groups {attached} -> [{sg_id}]")
                self.ec2.modify_instance_attribute(InstanceId=inst["InstanceId"], Groups=[sg_id])

        self.ec2.stop_instances(InstanceIds=iids)
        self.log(f"stopping {', '.join(iids)}")
        self.ec2.get_waiter("instance_stopped").wait(InstanceIds=iids)
        self.log(f"{', '.join(iids)} stopped")
        for iid in iids:
            upsert_manifest(self.manifest_path, self.account, self.region, iid, sg_id)

    def stop(self):
        ids = self.our_instances(["pending", "running"])
        if not ids:
            self.log("nothing running")
            return
        self.ec2.stop_instances(InstanceIds=ids)
        self.ec2.get_waiter("instance_stopped").wait(InstanceIds=ids)
        self.log(f"stopped {', '.join(ids)}")

    def status(self, show_tags=False):
        ids = self.instance_ids()
        if not ids:
            self.log("no instances")
            return
        resp = self.ec2.describe_instances(InstanceIds=ids)
        for res in resp["Reservations"]:
            for i in res["Instances"]:
                self.log(
                    f"{i['InstanceId']}  {i['InstanceType']}  {i['State']['Name']}  "
                    f"private={i.get('PrivateIpAddress', '-')}  "
                    f"public={i.get('PublicIpAddress', '-')}"
                )
                if show_tags:
                    tags = {t["Key"]: t["Value"] for t in i.get("Tags", [])}
                    self.log(f"    tags: {tags or '-'}")

    def terminate(self, instance_ids=None):
        ids = list(instance_ids) if instance_ids else self.instance_ids()
        if ids:
            self.log(f"terminating {', '.join(ids)}")
            try:
                self.ec2.terminate_instances(InstanceIds=ids, DryRun=self.dry_run)
            except ClientError as e:
                if e.response["Error"]["Code"] != "DryRunOperation":
                    raise
                self.log("dry run OK — the terminate would have succeeded")
                return
            self.ec2.get_waiter("instance_terminated").wait(InstanceIds=ids)
            self.log("terminated (root disks deleted with them)")
            remove_from_manifest(self.manifest_path, self.account, ids)
        else:
            self.log("no instances to terminate")
        # Only reap the shared no-traffic SG when nothing targeted was explicit —
        # an --instance-id terminate may be one of several instances in the account.
        if instance_ids or self.dry_run:
            return
        sgs = self.ec2.describe_security_groups(Filters=[
            {"Name": f"tag:{TAG_KEY}", "Values": [TAG_VALUE]},
        ])["SecurityGroups"]
        for sg in sgs:
            try:
                self.ec2.delete_security_group(GroupId=sg["GroupId"])
                self.log(f"deleted security group {sg['GroupId']}")
            except ClientError as e:
                self.log(f"could not delete security group {sg['GroupId']}: {e}")

    def apply_tags(self, adds, removes):
        ids = self.instance_ids()
        if not ids:
            self.log("no instances to tag")
            return
        try:
            if adds:
                self.ec2.create_tags(
                    Resources=ids, DryRun=self.dry_run,
                    Tags=[{"Key": k, "Value": v} for k, v in adds.items()],
                )
                self.log(f"added tags {adds} to {', '.join(ids)}")
            if removes:
                self.ec2.delete_tags(
                    Resources=ids, DryRun=self.dry_run,
                    Tags=[{"Key": k} for k in removes],
                )
                self.log(f"removed tags {removes} from {', '.join(ids)}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "DryRunOperation":
                raise
            self.log("dry run OK — the tag change would have succeeded")


def parse_tag_arg(spec):
    if "=" not in spec:
        sys.exit(f"--add-tag expects KEY=VALUE, got: {spec}")
    k, v = spec.split("=", 1)
    return k, v


def chunked(seq, size):
    if not size:
        yield seq
        return
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def run_account(r, args, tag_adds, tag_removes):
    """Dispatch one accounts.txt row. Returns (account, ok, error_message)."""
    tag = f"[{r['account']} {r['region']}]"
    try:
        acct = Account(r["account"], r["region"], r["profile"],
                        dry_run=args.dry_run, manifest_path=args.manifest)
        acct.verify_identity()
        if args.action == "create":
            acct.create(args.instance_type, count=args.count)
        elif args.action == "terminate":
            acct.terminate(instance_ids=args.instance_id)
        elif args.action == "tag":
            acct.apply_tags(tag_adds, tag_removes)
        elif args.action == "status":
            acct.status(show_tags=args.show_tags)
        else:
            getattr(acct, args.action)()
        return r["account"], True, None
    except AccountError as e:
        return r["account"], False, f"{tag} SKIPPED: {e}"
    except ClientError as e:
        return r["account"], False, f"{tag} AWS ERROR: {e}"
    except Exception as e:  # keep going with the other accounts
        return r["account"], False, f"{tag} ERROR: {type(e).__name__}: {e}"


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Locked-down EC2 instances across AWS accounts")
    p.add_argument("action", choices=["create", "status", "stop", "terminate", "tag"])
    p.add_argument("--accounts", default="accounts.txt")
    p.add_argument("--manifest", default="instances.csv")
    p.add_argument("--instance-type")
    p.add_argument("--count", type=int, default=1,
                    help="instances to create per account, action=create (default: 1)")
    p.add_argument("--only", nargs="+", metavar="ACCOUNT")
    p.add_argument("--show-tags", action="store_true", help="action=status: also print each instance's tags")
    p.add_argument("--instance-id", nargs="+", metavar="ID",
                    help="terminate exactly these instance ids (requires a single --only account)")
    p.add_argument("--region", help="override the region for --instance-id")
    p.add_argument("--add-tag", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--remove-tag", action="append", default=[], metavar="KEY")
    p.add_argument("--ramp-size", type=int, default=None,
                    help="apply `create`/`tag` action to N accounts at a time, in parallel "
                         "within each batch (default: all accounts at once, in parallel)")
    p.add_argument("--ramp-delay", type=float, default=30,
                    help="seconds to wait between ramp-up batches (default: 30)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true", help="skip terminate confirmation")
    args = p.parse_args()

    rows = read_accounts(args.accounts)
    if args.only:
        wanted = {a.zfill(12) for a in args.only}
        rows = [r for r in rows if r["account"] in wanted]
        if not rows:
            sys.exit("None of the --only accounts are in the accounts file")

    if args.instance_id and (args.action != "terminate" or len(rows) != 1):
        sys.exit("--instance-id requires action=terminate and exactly one --only account")
    if args.region:
        for r in rows:
            r["region"] = args.region

    tag_adds = dict(parse_tag_arg(s) for s in args.add_tag)
    tag_removes = list(args.remove_tag)
    if args.action == "tag" and not tag_adds and not tag_removes:
        sys.exit("tag action requires --add-tag and/or --remove-tag")

    if args.action == "terminate" and not args.yes:
        print("This will TERMINATE instances in:")
        for r in rows:
            target = ", ".join(args.instance_id) if args.instance_id else "all tracked instances"
            print(f"  {r['account']}  {r['region']}  (profile {r['profile']})  -> {target}")
        if input("Type 'yes' to continue: ").strip() != "yes":
            sys.exit("Aborted.")

    ok, failed = [], []
    for batch in chunked(rows, args.ramp_size if args.action in PARALLEL_ACTIONS else None):
        if args.action in PARALLEL_ACTIONS and args.ramp_size and batch is not rows and (ok or failed):
            print(f"\nramp-up: waiting {args.ramp_delay}s before the next batch ...")
            time.sleep(args.ramp_delay)

        if args.action in PARALLEL_ACTIONS:
            # Each account in a batch is independent (own session/client) — safe to parallelize.
            with ThreadPoolExecutor(max_workers=len(batch)) as ex:
                futs = [ex.submit(run_account, r, args, tag_adds, tag_removes) for r in batch]
                results = [fut.result() for fut in as_completed(futs)]
        else:
            results = [run_account(r, args, tag_adds, tag_removes) for r in batch]

        for account, success, error_message in results:
            if success:
                ok.append(account)
            else:
                print(error_message, flush=True)
                failed.append(account)

    print()
    print(f"'{args.action}' succeeded in {len(ok)} account(s): {', '.join(ok) or '-'}")
    if failed:
        print(f"Failed in {len(failed)} account(s): {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
