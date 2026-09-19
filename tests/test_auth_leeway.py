"""Regression tests for Cloudflare Access JWT validation.

Access mints the assertion with `iat` taken at the Cloudflare edge. When this
host's clock runs behind the edge -- a Docker Desktop / WSL2 VM drifting a
second or two is enough -- a zero-leeway check rejects the assertion as
"not yet valid (iat)" and callers see an intermittent 403 from
AccessMiddleware._authorize. CloudflareVerifier.verify therefore passes a
leeway, and these tests hold that behaviour in place.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from cisco_mcp.auth import CloudflareVerifier

AUD = "test-audience"


@dataclass(frozen=True)
class StubSettings:
    """Only the fields CloudflareVerifier actually reads."""

    cf_leeway_seconds: int = 60
    cf_aud: str = AUD
    cf_certs_url: str = "https://example.invalid/cdn-cgi/access/certs"


class _StubJWKClient:
    """Stands in for PyJWKClient so the tests never touch the network."""

    def __init__(self, public_key) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):  # noqa: ARG002
        return type("SigningKey", (), {"key": self._public_key})()


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signing_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return signing_key, key.public_key()


def _verifier(monkeypatch, public_key, leeway: int) -> CloudflareVerifier:
    v = CloudflareVerifier(StubSettings(cf_leeway_seconds=leeway))
    monkeypatch.setattr(v, "_client", lambda: _StubJWKClient(public_key))
    return v


def _assertion(signing_key, **claims) -> str:
    payload = {"aud": AUD, "common_name": "svc.access", **claims}
    return jwt.encode(payload, signing_key, algorithm="RS256")


def test_assertion_minted_ahead_of_our_clock_is_accepted(keypair, monkeypatch):
    """The actual bug: edge clock ahead of ours must not cause a 403."""
    signing_key, public_key = keypair
    token = _assertion(signing_key, iat=time.time() + 5)

    claims = _verifier(monkeypatch, public_key, leeway=60).verify(token)

    assert claims["common_name"] == "svc.access"


def test_zero_leeway_still_rejects_it(keypair, monkeypatch):
    """Guards the test above: without leeway this is exactly the 403 we saw."""
    signing_key, public_key = keypair
    token = _assertion(signing_key, iat=time.time() + 5)

    with pytest.raises(jwt.ImmatureSignatureError):
        _verifier(monkeypatch, public_key, leeway=0).verify(token)


def test_leeway_does_not_accept_a_genuinely_expired_assertion(keypair, monkeypatch):
    signing_key, public_key = keypair
    token = _assertion(signing_key, iat=time.time() - 7200, exp=time.time() - 3600)

    with pytest.raises(jwt.ExpiredSignatureError):
        _verifier(monkeypatch, public_key, leeway=60).verify(token)


def test_leeway_does_not_accept_a_wrong_audience(keypair, monkeypatch):
    signing_key, public_key = keypair
    token = jwt.encode(
        {"aud": "someone-elses-app", "common_name": "svc.access", "iat": time.time()},
        signing_key,
        algorithm="RS256",
    )

    with pytest.raises(jwt.InvalidAudienceError):
        _verifier(monkeypatch, public_key, leeway=60).verify(token)
