# Remote access with Cloudflare Tunnel and service tokens

This is the runbook for exposing the server to the internet without opening a
port, gated so that each user needs their own token and every call is logged.
Everything here fits on Cloudflare's free Zero Trust tier.

## How it fits together

```
MCP client ──(CF-Access-Client-Id + CF-Access-Client-Secret)──▶ Cloudflare edge
                                                                     │  Access checks the
                                                                     │  service token, then
                                                                     │  injects a signed JWT
                                                                     ▼
                                          cloudflared (in compose) ──▶ mcp:8000
                                                                     │  server verifies the JWT,
                                                                     │  maps it to a user, logs it
```

No inbound ports. The only path in is the tunnel, and Access will not pass a
request through without a valid service token. The server verifies the injected
JWT again, so even a direct hit on the origin is rejected.

## Prerequisites

- A domain on Cloudflare (any plan, including free).
- Zero Trust enabled on the account (free tier covers up to 50 users).
- This project running locally already (`docker compose up` works).

## Step 1 - Create the tunnel

In the Cloudflare **Zero Trust** dashboard, go to **Networks > Tunnels** and
create a tunnel of type **Cloudflared**. Copy the tunnel **token** it shows.

Put it in `.env`:

```
TUNNEL_TOKEN=eyJ... (the long token)
```

You do not run cloudflared by hand. The `cloudflared` service in the compose
file runs it for you from that token.

## Step 2 - Add a public hostname

Still in the tunnel configuration, add a **public hostname**.

- Subdomain and domain, for example `mcp.example.com`.
- Service type **HTTP**, URL **`mcp:8000`** (the compose service name and port).

## Step 3 - Create an Access application

Go to **Access > Applications > Add an application > Self-hosted**.

- Application domain is the same hostname, `mcp.example.com`.
- Save it, then open it again and copy the **Application Audience (AUD) Tag**.
  You will need it as `CF_ACCESS_AUD`.

## Step 4 - Add a Service Auth policy

On that application, add a policy.

- **Action** must be **Service Auth**. This is what makes Access accept a token
  instead of prompting a human to log in.
- In the rules, add the **Service Token** selector. Choose the specific tokens
  you will create in Step 5, or choose **Any Access Service Token** to accept any
  token in your account (the server's own allowlist can still narrow it).

## Step 5 - Create a service token per user

Go to **Access > Service Auth > Service Tokens** (in some dashboards this is
under **Access controls > Service credentials**). Create one token per user.

Cloudflare shows the **Client ID** and **Client Secret** once. Give each user
their pair. They set them as headers on their MCP client (Step 8).

You can also let the helper create tokens for you. Set `CF_API_TOKEN` (with
Access service-token edit permission) and `CF_ACCOUNT_ID`, then:

```bash
make add-user NAME="Alice" CREATE=1
```

## Step 6 - Find your team domain

Your team domain looks like `yourteam.cloudflareaccess.com`. It is shown under
**Settings** in Zero Trust. The server uses it to fetch the public keys that
verify the Access JWT.

## Step 7 - Configure the server for remote mode

In `.env`:

```
AUTH_MODE=cloudflare
CF_ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com
CF_ACCESS_AUD=<the AUD tag from Step 3>
ALLOW_ANY_TOKEN=false
```

Register each user's token so the audit log shows names. Copy the example file
and add entries (or use `make add-user`):

```bash
cp config/users.example.yaml config/users.yaml
make add-user NAME="Alice" CLIENT_ID="<client-id>.access"
make list-users
```

With `ALLOW_ANY_TOKEN=false`, only tokens listed in `config/users.yaml` are
accepted, even if Access would pass them.

## Step 8 - Launch with the tunnel

```bash
docker compose --profile tunnel up -d --build
```

The server now answers at `https://mcp.example.com/mcp`, protected by Access.

## Step 9 - Connect a client

Any MCP client that speaks streamable HTTP and can send custom headers works.
The two headers are the service token.

Claude Desktop (`claude_desktop_config.json`), using the `mcp-remote` shim:

```json
{
  "mcpServers": {
    "cisco-docs": {
      "command": "npx",
      "args": [
        "mcp-remote", "https://mcp.example.com/mcp",
        "--header", "CF-Access-Client-Id:${CF_ID}",
        "--header", "CF-Access-Client-Secret:${CF_SECRET}"
      ],
      "env": {
        "CF_ID": "<client-id>.access",
        "CF_SECRET": "<client-secret>"
      }
    }
  }
}
```

Claude Code:

```bash
claude mcp add --transport http cisco-docs https://mcp.example.com/mcp \
  --header "CF-Access-Client-Id: <client-id>.access" \
  --header "CF-Access-Client-Secret: <client-secret>"
```

## Managing users

- **Add** a user: create a token (Step 5) and register it (Step 7).
- **Disable** a user: set `enabled: false` for their entry in
  `config/users.yaml` and restart the `mcp` service, or revoke the token in
  Cloudflare. Revoking in Cloudflare stops it at the edge immediately.
- **Rotate** a secret: Cloudflare supports a rotation grace period. Issue the new
  secret, update the user, then invalidate the old one.

## Where the logs are

- **Your copy.** `data/logs/audit.log` (rotated) and the `mcp` container stdout.
  One JSON line per remote request and per tool call, with the user identity,
  tool, filters, result count, source IP, status, and latency.
- **Cloudflare's copy.** Zero Trust **Logs > Access** shows every authentication
  with the service token identity.

```bash
docker compose logs -f mcp                 # live
tail -f data/logs/audit.log 2>/dev/null    # if you bind-mount the logs volume
```

## Troubleshooting

- **403 from the server, "missing Cf-Access-Jwt-Assertion".** The request did not
  come through Access. Confirm the client is hitting the public hostname, not the
  origin directly.
- **403, "token not in users allowlist".** Add the token's Client ID to
  `config/users.yaml`, or set `ALLOW_ANY_TOKEN=true`.
- **403, "invalid token".** `CF_ACCESS_AUD` or `CF_ACCESS_TEAM_DOMAIN` is wrong,
  or the policy action is not Service Auth.
- **502 from Cloudflare.** The public hostname service should be `mcp:8000` and
  the `cloudflared` container must be on the same compose project.
