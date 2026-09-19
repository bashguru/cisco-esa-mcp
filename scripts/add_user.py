#!/usr/bin/env python3
"""Add a remote user (a Cloudflare Access service token) to the allowlist.

Two modes:

1) You already created the service token in the Cloudflare dashboard:
       python scripts/add_user.py --name "Alice" --client-id <CLIENT_ID>

2) Let this script create the service token for you via the Cloudflare API
   (needs CF_API_TOKEN with Access: Service Tokens edit, and CF_ACCOUNT_ID):
       python scripts/add_user.py --name "Alice" --create

The client SECRET is shown by Cloudflare exactly once. Hand it to the user with
the client ID; they set both as headers on their MCP client:
    CF-Access-Client-Id:     <CLIENT_ID>
    CF-Access-Client-Secret: <CLIENT_SECRET>

After adding a user you must also allow the token in your Access application
policy (action = Service Auth). See docs/CLOUDFLARE.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import yaml  # noqa: E402
from cisco_mcp.config import get_settings  # noqa: E402


def create_service_token(name: str) -> tuple[str, str]:
    account = os.environ["CF_ACCOUNT_ID"]
    api_token = os.environ["CF_API_TOKEN"]
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/access/service_tokens"
    req = urllib.request.Request(
        url,
        data=json.dumps({"name": name}).encode(),
        headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read())
    if not body.get("success"):
        raise RuntimeError(f"Cloudflare API error: {body.get('errors')}")
    result = body["result"]
    return result["client_id"], result["client_secret"]


def upsert_user(users_file: str, client_id: str, name: str) -> None:
    data = {"users": []}
    if os.path.exists(users_file):
        with open(users_file, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {"users": []}
    data.setdefault("users", [])
    for u in data["users"]:
        if u.get("client_id") == client_id:
            u["name"], u["enabled"] = name, True
            break
    else:
        data["users"].append({"name": name, "client_id": client_id, "enabled": True})
    os.makedirs(os.path.dirname(users_file) or ".", exist_ok=True)
    with open(users_file, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="Friendly name shown in audit logs.")
    ap.add_argument("--client-id", help="Existing service token Client ID.")
    ap.add_argument("--create", action="store_true", help="Create the token via the Cloudflare API.")
    ap.add_argument("--users-file", default=get_settings().users_file)
    args = ap.parse_args()

    secret = None
    if args.create:
        client_id, secret = create_service_token(args.name)
    elif args.client_id:
        client_id = args.client_id
    else:
        ap.error("Provide --client-id, or use --create with CF_API_TOKEN and CF_ACCOUNT_ID set.")

    upsert_user(args.users_file, client_id, args.name)
    print(f"Added user '{args.name}' -> {args.users_file}")
    print(f"  CF-Access-Client-Id:     {client_id}")
    if secret:
        print(f"  CF-Access-Client-Secret: {secret}   (shown once - store it now)")
    print("Next: add this token to your Access application policy (action = Service Auth).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
