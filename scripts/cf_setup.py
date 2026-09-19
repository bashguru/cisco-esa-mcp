#!/usr/bin/env python3
"""Provision Cloudflare Tunnel + Access (service tokens) for the MCP server.

Reads CF_API_TOKEN from .env, then creates (idempotently) a named tunnel, a
proxied DNS record, an Access self-hosted application with a service-token
policy, and one service token per user. Writes TUNNEL_TOKEN and CF_ACCESS_*
back into .env, fills config/users.yaml, and drops each user's client id +
secret into a local secrets file (never printed).

Run on the machine whose egress IP the API token allows (e.g. the Mac).
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import httpx
import yaml
from dotenv import load_dotenv

REPO = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(REPO / ".env")

TOKEN = os.environ.get("CF_API_TOKEN", "").strip()
if not TOKEN:
    sys.exit("CF_API_TOKEN not found in .env")

HOSTNAME = os.environ.get("CF_HOSTNAME", "cisco-docs.thecybersamaritans.com")
ZONE_NAME = os.environ.get("CF_ZONE", "thecybersamaritans.com")
SERVICE = os.environ.get("CF_SERVICE", "http://mcp:8000")
TUNNEL_NAME = os.environ.get("CF_TUNNEL_NAME", "cisco-docs-mcp")
APP_NAME = os.environ.get("CF_APP_NAME", "Cisco Docs MCP")
USERS = [u.strip() for u in os.environ.get(
    "CF_USERS", "David Titov,Dave Neilson,Mark Reynolds").split(",") if u.strip()]

API = "https://api.cloudflare.com/client/v4"
client = httpx.Client(timeout=30, headers={
    "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})


class CFError(Exception):
    pass


def cf(method, path, body=None, params=None):
    r = client.request(method, API + path, json=body, params=params)
    try:
        data = r.json()
    except Exception:
        raise CFError(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
    if not data.get("success", False):
        raise CFError(f"{method} {path}: {json.dumps(data.get('errors'))}")
    return data.get("result")


def set_env(path, updates):
    lines = path.read_text().splitlines() if path.exists() else []
    seen, out = set(), []
    for ln in lines:
        k = ln.split("=", 1)[0].strip() if ("=" in ln and not ln.lstrip().startswith("#")) else None
        if k in updates:
            out.append(f"{k}={updates[k]}")
            seen.add(k)
        else:
            out.append(ln)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    path.write_text("\n".join(out) + "\n")


def main():
    zones = cf("GET", "/zones", params={"name": ZONE_NAME})
    if not zones:
        raise CFError(f"zone {ZONE_NAME} not visible to token (need Zone:Read)")
    zone = zones[0]
    zone_id = zone["id"]
    account_id = zone["account"]["id"]
    print("account:", account_id, "zone:", zone_id)

    org = cf("GET", f"/accounts/{account_id}/access/organizations")
    team_domain = (org or {}).get("auth_domain")
    print("team_domain:", team_domain)

    tunnels = [t for t in (cf("GET", f"/accounts/{account_id}/cfd_tunnel") or [])
               if t.get("name") == TUNNEL_NAME and not t.get("deleted_at")]
    if tunnels:
        tunnel_id = tunnels[0]["id"]
        print("tunnel (reused):", tunnel_id)
    else:
        tunnel_id = cf("POST", f"/accounts/{account_id}/cfd_tunnel",
                       {"name": TUNNEL_NAME, "config_src": "cloudflare"})["id"]
        print("tunnel (created):", tunnel_id)
    tunnel_token = cf("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token")

    cf("PUT", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
       {"config": {"ingress": [
           {"hostname": HOSTNAME, "service": SERVICE},
           {"service": "http_status:404"}]}})
    print("ingress set")

    cname = f"{tunnel_id}.cfargotunnel.com"
    recs = cf("GET", f"/zones/{zone_id}/dns_records", params={"name": HOSTNAME})
    rec_body = {"type": "CNAME", "name": HOSTNAME, "content": cname, "proxied": True, "ttl": 1}
    if recs:
        cf("PUT", f"/zones/{zone_id}/dns_records/{recs[0]['id']}", rec_body)
        print("dns updated")
    else:
        cf("POST", f"/zones/{zone_id}/dns_records", rec_body)
        print("dns created")

    apps = cf("GET", f"/accounts/{account_id}/access/apps") or []
    app = next((a for a in apps if a.get("domain") == HOSTNAME), None)
    if app is None:
        app = cf("POST", f"/accounts/{account_id}/access/apps", {
            "name": APP_NAME, "domain": HOSTNAME, "type": "self_hosted",
            "session_duration": "24h", "app_launcher_visible": False})
        print("access app created")
    else:
        print("access app reused")
    app_id = app.get("uid") or app.get("id")
    app_aud = app.get("aud")
    print("app_id:", app_id, "aud:", app_aud)

    existing = {t["name"]: t for t in (cf("GET", f"/accounts/{account_id}/access/service_tokens") or [])}
    users = []
    for name in USERS:
        if name in existing:
            t = existing[name]
            users.append({"name": name, "id": t["id"], "client_id": t["client_id"],
                          "client_secret": None})
            print(f"service token reused: {name} (secret not re-shown)")
        else:
            t = cf("POST", f"/accounts/{account_id}/access/service_tokens", {"name": name})
            users.append({"name": name, "id": t["id"], "client_id": t["client_id"],
                          "client_secret": t.get("client_secret")})
            print(f"service token created: {name}")

    includes = [{"service_token": {"token_id": u["id"]}} for u in users]
    policy_body = {"name": "MCP service tokens", "decision": "non_identity", "include": includes}
    try:
        cf("POST", f"/accounts/{account_id}/access/apps/{app_id}/policies", policy_body)
        print("policy attached (app-scoped)")
    except CFError as e:
        print("app-scoped policy failed, trying reusable:", e)
        pol = cf("POST", f"/accounts/{account_id}/access/policies", policy_body)
        cf("PUT", f"/accounts/{account_id}/access/apps/{app_id}", {
            "name": APP_NAME, "domain": HOSTNAME, "type": "self_hosted",
            "session_duration": "24h", "policies": [pol["id"]]})
        print("policy attached (reusable)")

    set_env(REPO / ".env", {
        "AUTH_MODE": "cloudflare",
        "CF_ACCESS_TEAM_DOMAIN": team_domain or "",
        "CF_ACCESS_AUD": app_aud or "",
        "ALLOW_ANY_TOKEN": "false",
        "TUNNEL_TOKEN": tunnel_token,
    })
    (REPO / "config" / "users.yaml").write_text(yaml.safe_dump(
        {"users": [{"name": u["name"], "client_id": u["client_id"], "enabled": True}
                   for u in users]}, sort_keys=False))
    pathlib.Path("/tmp/cisco_mcp_user_secrets.json").write_text(json.dumps({
        "hostname": HOSTNAME, "team_domain": team_domain, "aud": app_aud,
        "users": users}, indent=2))
    print("DONE team=%s aud=%s users=%d" % (team_domain, app_aud, len(users)))


if __name__ == "__main__":
    try:
        main()
    except CFError as e:
        print("CF_ERROR:", e)
        sys.exit(1)
