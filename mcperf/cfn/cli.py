"""
AWS Resource Management Framework

Usage:
  python -m mcperf.cfn create                               # provision all enabled resources
  python -m mcperf.cfn scale --resource sns_topic --add 50  # add 50 more
  python -m mcperf.cfn delete --resource sns_topic           # delete stacks for one type
  python -m mcperf.cfn delete --all                          # delete everything
  python -m mcperf.cfn status                                # show current state
"""

import argparse

from mcperf.cfn.commands import cmd_create, cmd_delete, cmd_scale, cmd_status
from mcperf.cfn.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AWS Resource Management Framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("create", help="Provision all enabled resources from config.yaml")

    scale_p = sub.add_parser("scale", help="Add resources to a deployed type")
    scale_p.add_argument(
        "--resource", required=True,
        help="Resource type key from config.yaml (e.g. sns_topic)",
    )
    scale_p.add_argument(
        "--add", type=int, required=True,
        help="Number of additional resources to create",
    )

    del_p = sub.add_parser("delete", help="Delete CloudFormation stacks")
    del_p.add_argument("--resource", help="Delete stacks for this resource type only")
    del_p.add_argument(
        "--all", dest="delete_all", action="store_true",
        help="Delete ALL framework stacks",
    )

    sub.add_parser("status", help="Show deployed resource summary")

    args = parser.parse_args()
    cfg = load_config()

    if args.command == "create":
        cmd_create(cfg)
    elif args.command == "scale":
        cmd_scale(cfg, args.resource, args.add)
    elif args.command == "delete":
        if not args.resource and not args.delete_all:
            parser.error("delete requires --resource TYPE or --all")
        cmd_delete(cfg, args.resource, args.delete_all)
    elif args.command == "status":
        cmd_status(cfg)


if __name__ == "__main__":
    main()
