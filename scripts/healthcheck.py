#!/usr/bin/env python3
"""Container healthcheck: GET /healthz and exit non-zero on failure."""
import os
import sys
import urllib.request

port = os.getenv("MCP_PORT", "8000")
url = f"http://127.0.0.1:{port}/healthz"

try:
    with urllib.request.urlopen(url, timeout=5) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception as exc:  # noqa: BLE001
    print(f"healthcheck failed: {exc}", file=sys.stderr)
    sys.exit(1)
