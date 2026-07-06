"""
tests/test_api.py

Integration-style tests for the FastAPI layer — auth, middleware, and routes.
Uses TestClient; no real providers are loaded.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from jose import jwt

# ---------------------------------------------------------------------------
# auth.py tests
# ---------------------------------------------------------------------------


class TestCreateAccessToken:

    def test_token_contains_subject(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
        from auth import create_access_token

        token = create_access_token("user:ajay", ["completions:read"])
        payload = jwt.decode(token, "test-secret-key-fixture", algorithms=["HS256"])
        assert payload["sub"] == "user:ajay"

    def test_token_contains_scopes(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
        from auth import create_access_token

        token = create_access_token("svc:gateway", ["completions:write", "tasks:read"])
        payload = jwt.decode(token, "test-secret-key-fixture", algorithms=["HS256"])
        assert "completions:write" in payload["scopes"]
        assert "tasks:read" in payload["scopes"]

    def test_token_has_exp(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
        from auth import create_access_token

        token = create_access_token("user:test", [])
        payload = jwt.decode(token, "test-secret-key-fixture", algorithms=["HS256"])
        assert "exp" in payload

    def test_custom_expiry(self, monkeypatch):
        import time

        monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
        from auth import create_access_token

        token = create_access_token("u", [], expires_delta=timedelta(seconds=10))
        payload = jwt.decode(token, "test-secret-key-fixture", algorithms=["HS256"])
        assert payload["exp"] - payload["iat"] == pytest.approx(10, abs=2)


class TestDecodeToken:

    def test_valid_token_decodes(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
        from auth import create_access_token, decode_token

        token = create_access_token("user:ajay", ["admin"])
        payload = decode_token(token)
        assert payload.sub == "user:ajay"
        assert "admin" in payload.scopes

    def test_expired_token_raises_401(self, monkeypatch):
        from fastapi import HTTPException

        monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
        from auth import create_access_token, decode_token

        token = create_access_token("u", [], expires_delta=timedelta(seconds=-1))
        with pytest.raises(HTTPException) as exc_info:
            decode_token(token)
        assert exc_info.value.status_code == 401

    def test_tampered_token_raises_401(self, monkeypatch):
        from fastapi import HTTPException

        monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
        from auth import decode_token

        with pytest.raises(HTTPException) as exc_info:
            decode_token("not.a.valid.token")
        assert exc_info.value.status_code == 401


class TestValidateApiKey:

    def test_valid_key_returns_context(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
        import hashlib
        from auth import validate_api_key

        raw = "cai-R7mNqP2wLkT9vX4hF"
        h = hashlib.sha256(raw.encode()).hexdigest()
        store = {h: {"owner": "ajay", "scopes": ["completions:write"]}}
        ctx = validate_api_key(raw, store)
        assert ctx is not None
        assert ctx.subject == "ajay"
        assert ctx.is_api_key is True

    def test_unknown_key_returns_none(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
        from auth import validate_api_key

        ctx = validate_api_key("cai-badkey", {})
        assert ctx is None


class TestAuthContext:

    def test_require_scope_passes_with_matching_scope(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
        from auth import AuthContext

        ctx = AuthContext(subject="ajay", scopes=["completions:write"])
        ctx.require_scope("completions:write")  # should not raise

    def test_require_scope_passes_with_admin(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
        from auth import AuthContext

        ctx = AuthContext(subject="ajay", scopes=["admin"])
        ctx.require_scope("completions:write")  # admin overrides

    def test_require_scope_raises_403_on_missing(self, monkeypatch):
        from fastapi import HTTPException

        monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
        from auth import AuthContext

        ctx = AuthContext(subject="limited", scopes=["completions:read"])
        with pytest.raises(HTTPException) as exc_info:
            ctx.require_scope("completions:write")
        assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# middleware.py tests
# ---------------------------------------------------------------------------


class TestRateLimitMiddleware:

    def test_check_rate_limit_allows_under_limit(self):
        from middleware import RateLimitMiddleware

        mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
        mw.rpm = 5
        mw._windows = {}
        for _ in range(4):
            allowed, _ = mw._check_rate_limit("ip:1.2.3.4")
            assert allowed is True

    def test_check_rate_limit_blocks_at_limit(self):
        from middleware import RateLimitMiddleware

        mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
        mw.rpm = 3
        mw._windows = {}
        for _ in range(3):
            mw._check_rate_limit("ip:5.6.7.8")
        allowed, retry_after = mw._check_rate_limit("ip:5.6.7.8")
        assert allowed is False
        assert retry_after > 0

    def test_remaining_decreases_with_requests(self):
        from middleware import RateLimitMiddleware

        mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
        mw.rpm = 10
        mw._windows = {}
        mw._check_rate_limit("ip:9.9.9.9")
        mw._check_rate_limit("ip:9.9.9.9")
        remaining = mw._remaining("ip:9.9.9.9")
        assert remaining == 8

    def test_api_key_used_as_client_key(self):
        from middleware import RateLimitMiddleware

        mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
        req = MagicMock()
        req.headers.get = lambda k: (
            "cai-R7mNqP2wLkT9vX4hF" if k == "X-API-Key" else None
        )
        key = mw._get_client_key(req)
        assert key.startswith("key:")

    def test_ip_used_as_fallback_client_key(self):
        from middleware import RateLimitMiddleware

        mw = RateLimitMiddleware.__new__(RateLimitMiddleware)
        req = MagicMock()
        req.headers.get = lambda k: None
        req.client.host = "10.0.0.1"
        key = mw._get_client_key(req)
        assert key == "ip:10.0.0.1"


# ---------------------------------------------------------------------------
# HMAC webhook signing
# ---------------------------------------------------------------------------


class TestWebhookSigning:

    def test_valid_signature_returns_true(self, monkeypatch):
        import hashlib
        import hmac
        import time

        monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
        monkeypatch.setenv("WEBHOOK_SIGNING_SECRET", "webhook-secret-fixture")
        from auth import verify_request_signature

        payload = b'{"event": "task.completed"}'
        ts = str(int(time.time()))
        signed = f"{ts}.{payload.decode()}".encode()
        sig = hmac.new(b"webhook-secret-fixture", signed, hashlib.sha256).hexdigest()
        assert verify_request_signature(payload, sig, ts) is True

    def test_wrong_signature_returns_false(self, monkeypatch):
        import time

        monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
        monkeypatch.setenv("WEBHOOK_SIGNING_SECRET", "webhook-secret-fixture")
        from auth import verify_request_signature

        payload = b'{"event": "task.completed"}'
        ts = str(int(time.time()))
        assert verify_request_signature(payload, "badsignature", ts) is False

    def test_stale_timestamp_returns_false(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
        monkeypatch.setenv("WEBHOOK_SIGNING_SECRET", "webhook-secret-fixture")
        from auth import verify_request_signature

        old_ts = str(int(1_000_000))  # very old
        assert verify_request_signature(b"payload", "sig", old_ts) is False

    def test_invalid_timestamp_returns_false(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixture")
        monkeypatch.setenv("WEBHOOK_SIGNING_SECRET", "webhook-secret-fixture")
        from auth import verify_request_signature

        assert verify_request_signature(b"data", "sig", "not-a-number") is False
