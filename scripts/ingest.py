#!/usr/bin/env python3
"""Convenience wrapper around `python -m cisco_mcp.ingest`.

Usage:
    python scripts/ingest.py [PATH]      # PATH defaults to $INPUT_DIR (/data/input)

Inside Docker:
    docker compose run --rm mcp python scripts/ingest.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cisco_mcp.ingest import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
