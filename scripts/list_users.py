#!/usr/bin/env python3
"""Print the configured remote users (service-token identities)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cisco_mcp.auth import load_users  # noqa: E402
from cisco_mcp.config import get_settings  # noqa: E402

users = load_users(get_settings().users_file)
if not users:
    print("No users configured. Add one with scripts/add_user.py.")
    sys.exit(0)

print(f"{'CLIENT ID / EMAIL':50}  {'NAME':24}  ENABLED")
print("-" * 90)
for key, meta in users.items():
    print(f"{key:50}  {meta['name']:24}  {meta['enabled']}")
