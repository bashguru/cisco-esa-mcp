"""Authentication + audit middleware.

Local mode  (AUTH_MODE=local)      -> no auth, pass through, bind to localhost.
Remote mode (AUTH_MODE=cloudflare) -> require a valid Cloudflare Access
    service-token JWT (the Cf-Access-Jwt-Assertion header injected by Access),
    map it to a named user from config/users.yaml, reject anything else, and
    write an audit line for every request.

This is a pure-ASGI middleware on purpose: MCP streamable HTTP uses long-lived
streaming responses, which Starlette's BaseHTTPMiddleware would buffer/break.
"""

from __future__ import annotations

import json
import threading
import time
from contextvars import ContextVar
from typing import Any, Optional

from .config import Settings, get_settings
from .logging_setup import audit_event, get_logger

log = get_logger(__name__)

# Identity of the caller for the current request (used by per-tool audit lines).
current_identity: ContextVar[str] = ContextVar("current_identity", default="local")


def get_identity() -> str:
    return current_identity.get()


# --------------------------------------------------------------------------
# Users allowlist (config/users.yaml)
# --------------------------------------------------------------------------
def load_users(path: str) -> dict[str, dict[str, Any]]:
    """Return {client_id_or_email: {"name": str, "enabled": bool}}."""
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read users file %s: %s", path, exc)
        return {}

    users = {}
    for entry in data.get("users", []) or []:
        key = entry.get("client_id") or entry.get("email")
        if not key:
            continue
        users[key] = {"name": entry.get("name", key), "enabled": entry.get("enabled", True)}
    return users


# --------------------------------------------------------------------------
# Cloudflare Access JWT verification
# --------------------------------------------------------------------------
class CloudflareVerifier:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._jwks = None
        self._lock = threading.Lock()

    def _client(self):
        if self._jwks is None:
            with self._lock:
                if self._jwks is None:
                    from jwt import PyJWKClient

                    self._jwks = PyJWKClient(self.s.cf_certs_url)
        return self._jwks

    def verify(self, token: str) -> dict[str, Any]:
        import jwt

        signing_key = self._client().get_signing_key_from_jwt(token)
        options = {"verify_aud": bool(self.s.cf_aud)}
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=self.s.cf_aud or None,
            options=options,
        )


# --------------------------------------------------------------------------
# ASGI middleware
# --------------------------------------------------------------------------
class AccessMiddleware:
    def __init__(self, app) -> None:
        self.app = app
        self.s = get_settings()
        self.users = load_users(self.s.users_file) if self.s.is_remote else {}
        self.verifier = CloudflareVerifier(self.s) if self.s.is_remote else None
        if self.s.is_remote:
            if not self.s.cf_team_domain or not self.s.cf_aud:
                log.warning(
                    "AUTH_MODE=cloudflare but CF_ACCESS_TEAM_DOMAIN / CF_ACCESS_AUD are unset; "
                    "requests will be denied until configured."
                )
            log.info("Remote auth enabled: %d configured user(s), allow_any_token=%s",
                     len(self.users), self.s.allow_any_token)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path.rstrip("/") == "/healthz":
            await self._respond(send, 200, {"status": "ok"})
            return

        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
        started = time.monotonic()
        identity = "local"

        if self.s.is_remote:
            ok, identity, reason = self._authorize(headers.get("cf-access-jwt-assertion"))
            if not ok:
                audit_event("auth_denied", reason=reason, path=path,
                            ip=headers.get("cf-connecting-ip"),
                            user_agent=headers.get("user-agent"))
                await self._respond(send, 403, {"error": "forbidden", "reason": reason})
                return

        token_ctx = current_identity.set(identity)
        status_holder: dict[str, int] = {}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            current_identity.reset(token_ctx)
            if self.s.is_remote:
                audit_event(
                    "http_request",
                    identity=identity,
                    method=scope.get("method"),
                    path=path,
                    status=status_holder.get("status"),
                    ip=headers.get("cf-connecting-ip"),
                    latency_ms=round((time.monotonic() - started) * 1000, 1),
                )

    def _authorize(self, token: Optional[str]) -> tuple[bool, str, str]:
        if not token:
            return False, "-", "missing Cf-Access-Jwt-Assertion header"
        try:
            claims = self.verifier.verify(token)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            return False, "-", f"invalid token: {exc}"
        key = claims.get("common_name") or claims.get("email") or claims.get("sub") or "-"
        entry = self.users.get(key)
        if entry is not None:
            if not entry.get("enabled", True):
                return False, entry.get("name", key), "user disabled"
            return True, entry.get("name", key), ""
        if self.s.allow_any_token:
            return True, str(key), ""
        return False, str(key), "token not in users allowlist"

    @staticmethod
    async def _respond(send, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(payload)).encode())],
        })
        await send({"type": "http.response.body", "body": payload})
