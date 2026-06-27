"""API Key authentication middleware.

Validates ``Authorization: Bearer <token>`` headers against a configured
API key (stored in ``config.json``).  Uses SHA-256 constant-time comparison
to prevent timing attacks.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from uniharness.exceptions import ErrorCode
from uniharness_api.models import ErrorResponse

logger = logging.getLogger(__name__)

# Paths that never require authentication.
_WHITELIST_PREFIXES: tuple[str, ...] = (
    "/health",
    "/api/setup",
    "/api/ws",
)

# Static whitelisted paths.
_WHITELIST_PATHS: frozenset[str] = frozenset({
    "/",
    "/docs",
    "/openapi.json",
    "/redoc",
})


def _is_whitelisted(path: str) -> bool:
    """Return True if the path does not require authentication."""
    if path in _WHITELIST_PATHS:
        return True
    return path.startswith(_WHITELIST_PREFIXES)


def _verify_token(provided: str, expected: str) -> bool:
    """Constant-time token comparison via SHA-256."""
    if not expected:
        return True  # Auth disabled — allow all
    return hmac.compare_digest(
        hashlib.sha256(provided.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    )


def _extract_bearer_token(request: Request) -> str | None:
    """Extract Bearer token from the Authorization header, or None."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


class AuthMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware that enforces API key authentication.

    Configured via ``config.json`` ``api_key`` field.  When the key is
    empty, authentication is disabled (all paths are open).

    Whitelisted paths (``/health``, ``/api/setup/``, etc.) skip
    authentication regardless of configuration.
    """

    def __init__(
        self,
        app,
        *,
        api_key: str = "",
    ) -> None:
        super().__init__(app)
        self._api_key = api_key

    def set_api_key(self, key: str) -> None:
        """Update the API key at runtime (e.g. after config change)."""
        self._api_key = key

    @property
    def is_enabled(self) -> bool:
        """Whether authentication is active."""
        return bool(self._api_key)

    async def dispatch(  # type: ignore[override]
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not self._api_key or _is_whitelisted(request.url.path):
            return await call_next(request)

        token = _extract_bearer_token(request)
        if token is None or not _verify_token(token, self._api_key):
            err = ErrorResponse(
                error_code=ErrorCode.LLM_AUTH_FAILED.value,
                message="Invalid or missing API key",
                retryable=False,
            )
            return Response(
                content=err.model_dump_json(),
                status_code=401,
                media_type="application/json",
            )

        return await call_next(request)
