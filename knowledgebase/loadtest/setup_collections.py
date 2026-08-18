"""Stage 1 — create KB collections under an MSP, ready for load-test uploads.

    export KB_ROOT_PASSWORD=<msp root password>
    python setup_collections.py --config collections_config.yaml
"""
import argparse
import datetime
import os
import sys

import yaml

from auth import fetch_org_id, signin
from client import KBClient, base_url_for_env


def parse_args():
    p = argparse.ArgumentParser(description="Create KB collections for a load-test run.")
    p.add_argument("--config", default="collections_config.yaml",
                   help="YAML config with env, root creds, cookies and collections "
                        "(default: collections_config.yaml)")
    p.add_argument("--root-password", default=os.environ.get("KB_ROOT_PASSWORD"),
                   help="MSP root password (or set KB_ROOT_PASSWORD); not stored in the YAML")
    p.add_argument("--request-timeout", type=int, default=30,
                   help="Per-request timeout seconds (default: 30)")
    p.add_argument("--output", default=None,
                   help="Output YAML path (default: collections_output_<timestamp>.yaml)")
    return p.parse_args()


def load_config(path):
    if not os.path.isfile(path):
        sys.exit(f"Config file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if not cfg.get("env"):
        sys.exit("Config missing required 'env' key.")
    if not cfg.get("root", {}).get("email"):
        sys.exit("Config missing required 'root.email' key.")
    if not cfg.get("collections"):
        sys.exit("Config has no 'collections' to create.")
    return cfg


def create_collections(client, specs):
    results = []
    for spec in specs:
        body = {
            "name": spec["name"],
            "description": spec.get("description", ""),
            "category": spec.get("category", "tenant"),
        }
        resp = client.create_collection(body)
        if resp.status_code not in (200, 201):
            print(f"  FAILED '{spec['name']}' (HTTP {resp.status_code}): {resp.text[:300]}")
            continue
        data = (resp.json() or {}).get("data") or {}
        results.append({
            "name": data.get("name", spec["name"]),
            "id": data.get("id"),
            "customer_id": data.get("customer_id"),
            "organization_id": data.get("organization_id"),
        })
        print(f"  created '{spec['name']}' -> {data.get('id')}")
    return results


def main():
    args = parse_args()
    cfg = load_config(args.config)
    if not args.root_password:
        sys.exit("No root password: pass --root-password or set KB_ROOT_PASSWORD.")

    env = cfg["env"]
    mc_debug_mode = cfg.get("mc_debug_mode", True)
    print(f"Signing in as {cfg['root']['email']} ({env}) ...")
    token = signin(env, cfg["root"]["email"], args.root_password, mc_debug_mode,
                   args.request_timeout)
    org_id = fetch_org_id(env, token, args.request_timeout)
    print(f"  OrganizationId = {org_id}")

    client = KBClient(base_url_for_env(env), token, org_id=org_id,
                       timeout=args.request_timeout)  # cookies=cfg.get("cookies") disabled for now
    print(f"Creating {len(cfg['collections'])} collection(s) ...")
    results = create_collections(client, cfg["collections"])

    output = args.output or f"collections_output_{datetime.datetime.now():%Y%m%d_%H%M%S}.yaml"
    with open(output, "w", encoding="utf-8") as fh:
        yaml.safe_dump({"collections": results}, fh, sort_keys=False)

    ids = ",".join(r["id"] for r in results if r.get("id"))
    print(f"\n{len(results)}/{len(cfg['collections'])} collection(s) created.")
    print(f"Output: {output}")
    print(f"--collection-id {ids}")


if __name__ == "__main__":
    main()
