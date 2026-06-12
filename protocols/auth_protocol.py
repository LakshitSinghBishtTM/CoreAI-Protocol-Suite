"""
protocols/auth_protocol.py

CoreAI Authentication Protocol layer.
Handles API key validation, JWT issuance, HMAC request signing,
and per-client permission scoping for the CoreAI API surface.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

JWT_SECRET = os.environ.get("COREAI_JWT_SECRET", "")
API_KEY_PREFIX = "cai-"
TOKEN_TTL_S = int(os.environ.get("COREAI_TOKEN_TTL", "3600"))
HMAC_ALGORITHM = "sha256"
MAX_CLOCK_SKEW_S = 30


# ---------------------------------------------------------------------------
# Enums / scopes
# ---------------------------------------------------------------------------


class AuthScope(str, Enum):
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"
    AGENT = "agent"
    STREAM = "stream"


class AuthStatus(str, Enum):
    VALID = "valid"
    EXPIRED = "expired"
    INVALID = "invalid"
    REVOKED = "revoked"
    RATE_LIMITED = "rate_limited"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class APICredential:
    client_id: str
    api_key: str
    scopes: list[AuthScope] = field(default_factory=lambda: [AuthScope.READ])
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    revoked: bool = False
    metadata: dict = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    @property
    def key_prefix(self) -> str:
        return self.api_key[:12] + "..."

    def has_scope(self, scope: AuthScope) -> bool:
        return scope in self.scopes or AuthScope.ADMIN in self.scopes


@dataclass
class AuthToken:
    token_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    client_id: str = ""
    scopes: list[AuthScope] = field(default_factory=list)
    issued_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + TOKEN_TTL_S)
    signature: str = ""

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def ttl_remaining_s(self) -> float:
        return max(0.0, self.expires_at - time.time())


@dataclass
class AuthResult:
    status: AuthStatus
    client_id: Optional[str] = None
    scopes: list[AuthScope] = field(default_factory=list)
    token: Optional[AuthToken] = None
    reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == AuthStatus.VALID


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AuthenticationError(Exception):
    """Raised on invalid credentials or tampered requests."""


class AuthorizationError(Exception):
    """Raised when a valid credential lacks required scope."""


class TokenExpiredError(AuthenticationError):
    """Raised when a token is presented past its TTL."""


# ---------------------------------------------------------------------------
# HMAC request signing
# ---------------------------------------------------------------------------


class RequestSigner:
    """
    Signs and verifies CoreAI API requests using HMAC-SHA256.

    Signing scheme:
        message = "{method}\\n{path}\\n{timestamp}\\n{body_hash}"
        signature = HMAC-SHA256(secret, message)

    The signature is passed as the X-CoreAI-Signature header.
    Timestamp must be within MAX_CLOCK_SKEW_S of server time.
    """

    def __init__(self, secret: str):
        if not secret:
            raise ValueError("Signing secret must not be empty")
        self._secret = secret.encode()

    def sign(self, method: str, path: str, body: bytes = b"") -> dict[str, str]:
        timestamp = str(int(time.time()))
        body_hash = hashlib.sha256(body).hexdigest()
        message = f"{method.upper()}\n{path}\n{timestamp}\n{body_hash}"
        sig = hmac.new(self._secret, message.encode(), HMAC_ALGORITHM).hexdigest()
        return {
            "X-CoreAI-Timestamp": timestamp,
            "X-CoreAI-Signature": sig,
            "X-CoreAI-Body-Hash": body_hash,
        }

    def verify(
        self,
        method: str,
        path: str,
        timestamp: str,
        signature: str,
        body: bytes = b"",
    ) -> bool:
        try:
            ts = int(timestamp)
        except (ValueError, TypeError):
            return False

        if abs(time.time() - ts) > MAX_CLOCK_SKEW_S:
            logger.warning("Request timestamp outside clock skew window: %s", timestamp)
            return False

        body_hash = hashlib.sha256(body).hexdigest()
        message = f"{method.upper()}\n{path}\n{timestamp}\n{body_hash}"
        expected = hmac.new(self._secret, message.encode(), HMAC_ALGORITHM).hexdigest()
        return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Token issuer
# ---------------------------------------------------------------------------


class TokenIssuer:
    """
    Issues and validates short-lived bearer tokens backed by HMAC-SHA256.
    Not a full JWT implementation — uses a lightweight signed payload.
    """

    def __init__(self, secret: str = JWT_SECRET):
        if not secret:
            logger.warning("JWT_SECRET not set — token auth disabled")
        self._secret = secret.encode() if secret else b""

    def issue(self, client_id: str, scopes: list[AuthScope]) -> AuthToken:
        token = AuthToken(client_id=client_id, scopes=scopes)
        payload = f"{token.token_id}:{client_id}:{token.issued_at}:{token.expires_at}"
        token.signature = hmac.new(
            self._secret, payload.encode(), HMAC_ALGORITHM
        ).hexdigest()
        logger.debug("Token issued for %s (ttl=%ds)", client_id, TOKEN_TTL_S)
        return token

    def verify(self, token: AuthToken) -> AuthResult:
        if token.is_expired:
            return AuthResult(
                status=AuthStatus.EXPIRED,
                client_id=token.client_id,
                reason="Token expired",
            )

        payload = f"{token.token_id}:{token.client_id}:{token.issued_at}:{token.expires_at}"
        expected = hmac.new(
            self._secret, payload.encode(), HMAC_ALGORITHM
        ).hexdigest()

        if not hmac.compare_digest(expected, token.signature):
            return AuthResult(status=AuthStatus.INVALID, reason="Signature mismatch")

        return AuthResult(
            status=AuthStatus.VALID,
            client_id=token.client_id,
            scopes=token.scopes,
            token=token,
        )


# ---------------------------------------------------------------------------
# Auth protocol
# ---------------------------------------------------------------------------


class AuthProtocol:
    """
    Main authentication/authorisation entry point for CoreAI API handlers.

    Usage::

        proto = AuthProtocol()
        proto.register("client-abc", "cai-xK9mP...", scopes=[AuthScope.WRITE])

        result = proto.authenticate("cai-xK9mP...")
        if not result.ok:
            raise AuthenticationError(result.reason)

        proto.require_scope(result, AuthScope.WRITE)
    """

    def __init__(self, signer_secret: str = "", token_secret: str = JWT_SECRET):
        self._credentials: dict[str, APICredential] = {}
        self._revoked_tokens: set[str] = set()
        self._signer = RequestSigner(signer_secret) if signer_secret else None
        self._issuer = TokenIssuer(token_secret)

    # ------------------------------------------------------------------
    # Credential management
    # ------------------------------------------------------------------

    def register(
        self,
        client_id: str,
        api_key: str,
        scopes: Optional[list[AuthScope]] = None,
        expires_in_s: Optional[int] = None,
        metadata: Optional[dict] = None,
    ) -> APICredential:
        if not api_key.startswith(API_KEY_PREFIX):
            raise ValueError(f"API key must start with '{API_KEY_PREFIX}'")

        expires_at = time.time() + expires_in_s if expires_in_s else None
        cred = APICredential(
            client_id=client_id,
            api_key=api_key,
            scopes=scopes or [AuthScope.READ],
            expires_at=expires_at,
            metadata=metadata or {},
        )
        self._credentials[api_key] = cred
        logger.info("Credential registered: %s (%s)", client_id, cred.key_prefix)
        return cred

    def revoke(self, api_key: str) -> None:
        if api_key in self._credentials:
            self._credentials[api_key].revoked = True
            logger.warning("Credential revoked: %s", self._credentials[api_key].key_prefix)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self, api_key: str) -> AuthResult:
        cred = self._credentials.get(api_key)

        if cred is None:
            return AuthResult(status=AuthStatus.INVALID, reason="Unknown API key")

        if cred.revoked:
            return AuthResult(
                status=AuthStatus.REVOKED,
                client_id=cred.client_id,
                reason="Credential has been revoked",
            )

        if cred.is_expired:
            return AuthResult(
                status=AuthStatus.EXPIRED,
                client_id=cred.client_id,
                reason="Credential expired",
            )

        token = self._issuer.issue(cred.client_id, cred.scopes)

        return AuthResult(
            status=AuthStatus.VALID,
            client_id=cred.client_id,
            scopes=cred.scopes,
            token=token,
        )

    def authenticate_token(self, token: AuthToken) -> AuthResult:
        if token.token_id in self._revoked_tokens:
            return AuthResult(
                status=AuthStatus.REVOKED,
                client_id=token.client_id,
                reason="Token revoked",
            )
        return self._issuer.verify(token)

    def revoke_token(self, token_id: str) -> None:
        self._revoked_tokens.add(token_id)
        logger.info("Token revoked: %s", token_id[:8])

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------

    def require_scope(self, result: AuthResult, scope: AuthScope) -> None:
        if not result.ok:
            raise AuthenticationError(f"Authentication failed: {result.reason}")
        if scope not in result.scopes and AuthScope.ADMIN not in result.scopes:
            raise AuthorizationError(
                f"Client '{result.client_id}' lacks required scope '{scope}'"
            )

    # ------------------------------------------------------------------
    # Request signing helpers
    # ------------------------------------------------------------------

    def sign_request(self, method: str, path: str, body: bytes = b"") -> dict[str, str]:
        if self._signer is None:
            raise RuntimeError("Request signer not configured (no secret provided)")
        return self._signer.sign(method, path, body)

    def verify_request(
        self, method: str, path: str, timestamp: str, signature: str, body: bytes = b""
    ) -> bool:
        if self._signer is None:
            return True  # signing not enforced
        return self._signer.verify(method, path, timestamp, signature, body)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_proto: Optional[AuthProtocol] = None


def get_auth_protocol() -> AuthProtocol:
    global _proto
    if _proto is None:
        secret = os.environ.get("COREAI_SIGNING_SECRET", "")
        _proto = AuthProtocol(signer_secret=secret)
    return _proto
