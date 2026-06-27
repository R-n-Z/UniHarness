"""In-memory sliding-window rate limiter for the UniHarness API.

Limits LLM call frequency per conversation to prevent abuse.
The limiter is intentionally in-process (no Redis dependency) — it
resets on server restart, which is acceptable for a local/desktop app.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from uniharness.exceptions import ErrorCode
from uniharness_api.models import ErrorResponse

logger = logging.getLogger(__name__)

# Default: 30 LLM calls per minute per conversation.
_DEFAULT_MAX_REQUESTS = 30
_DEFAULT_WINDOW_SECONDS = 60


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window rate limiter keyed by conversation_id.

    Only applies to chat message endpoints (``/api/chat/{id}/message``).
    Extracts the conversation_id from the URL path.
    """

    def __init__(
        self,
        app,
        *,
        max_requests: int = _DEFAULT_MAX_REQUESTS,
        window_seconds: float = _DEFAULT_WINDOW_SECONDS,
    ) -> None:
        super().__init__(app)
        self._max_requests = max_requests
        self._window = window_seconds
        # conversation_id -> list of Unix timestamps
        self._buckets: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def _should_rate_limit(self, conversation_id: str) -> bool:
        """Return True if the conversation has exceeded the rate limit."""
        now = time.monotonic()
        async with self._lock:
            # Prune expired timestamps
            cutoff = now - self._window
            self._buckets[conversation_id] = [
                t for t in self._buckets[conversation_id] if t > cutoff
            ]
            if len(self._buckets[conversation_id]) >= self._max_requests:
                return True
            self._buckets[conversation_id].append(now)
            return False

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path
        # Only rate-limit chat message endpoints
        if not ("/api/chat/" in path and "/message" in path):
            return await call_next(request)

        # Extract conversation_id from URL: /api/chat/{id}/message
        parts = path.split("/")
        try:
            msg_idx = parts.index("message")
            conversation_id = parts[msg_idx - 1]
        except (ValueError, IndexError):
            return await call_next(request)

        if await self._should_rate_limit(conversation_id):
            err = ErrorResponse(
                error_code=ErrorCode.LLM_RATE_LIMITED.value,
                message=f"Rate limit exceeded: {self._max_requests} requests per {self._window}s",
                retryable=True,
            )
            return Response(
                content=err.model_dump_json(),
                status_code=429,
                media_type="application/json",
            )

        return await call_next(request)
