"""Structured application and audit logging.

Two loggers:

* ``cisco_mcp``       - ordinary application log (stdout).
* ``cisco_mcp.audit`` - one JSON line per remote/tool event, to stdout AND to a
                        rotating file so there is a durable, tamper-evident-ish
                        trail of who called what. This is the "log any remote
                        use" requirement.

The audit trail is intentionally independent of Cloudflare's own Access logs,
so you keep a copy you control even on the free Zero Trust tier.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
from typing import Any

from .config import get_settings

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "audit", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    s = get_settings()

    root = logging.getLogger("cisco_mcp")
    root.setLevel(getattr(logging, s.log_level, logging.INFO))
    root.propagate = False

    stream = logging.StreamHandler()
    stream.setFormatter(_JsonFormatter() if s.log_json else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    ))
    root.handlers[:] = [stream]

    audit = logging.getLogger("cisco_mcp.audit")
    audit.setLevel(logging.INFO)
    audit.propagate = False
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        os.makedirs(os.path.dirname(s.audit_log_file), exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                s.audit_log_file, maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8"
            )
        )
    except OSError:
        # No writable audit dir (e.g. read-only local run) - stdout still works.
        pass
    for h in handlers:
        h.setFormatter(_JsonFormatter())
    audit.handlers[:] = handlers

    _CONFIGURED = True


def get_logger(name: str = "cisco_mcp") -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def audit_event(event: str, **fields: Any) -> None:
    """Write one structured audit line."""
    setup_logging()
    logging.getLogger("cisco_mcp.audit").info(event, extra={"audit": {"event": event, **fields}})
