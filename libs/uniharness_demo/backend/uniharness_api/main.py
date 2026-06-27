"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from contextvars import ContextVar

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from uniharness_api.agent_manager import agent_manager
from uniharness_api.database import init_db
from uniharness_api.routes import chat, config, conversations, sessions, setup, skills

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# -- Trace ID ----------------------------------------------------------------
# Context variable accessible from any async task in the request chain.

_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")


def get_trace_id() -> str:
    """Return the trace_id for the current request, or empty string."""
    return _trace_id_var.get()


class TraceContextMiddleware(BaseHTTPMiddleware):
    """Extract or generate a trace_id for every HTTP request.

    Reads from ``X-Trace-ID`` header; generates a ``uuid4`` if absent.
    Stores in ``_trace_id_var`` so downstream code (routes, audit log)
    can retrieve it without threading it through every function signature.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        trace_id = request.headers.get("X-Trace-ID", str(uuid.uuid4()))
        _trace_id_var.set(trace_id)
        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        return response


async def _cleanup_expired_sessions() -> None:
    """Periodically tear down unclaimed warm sessions."""
    import asyncio
    import shutil

    from uniharness_api.paths import uploads_dir
    from uniharness_api.store import session_store

    ul_dir = uploads_dir()

    while True:
        await asyncio.sleep(300)  # every 5 minutes
        try:
            for session in await session_store.expired(max_age_seconds=600):
                logger.info("Cleaning up expired warm session: %s", session.id)
                await agent_manager.teardown_session(session.mode, session.session_name)
                await session_store.delete(session.id)
                # Clean up any upload files left on disk
                session_uploads = ul_dir / session.id
                if session_uploads.is_dir():
                    shutil.rmtree(session_uploads)
        except Exception:
            logger.exception("Error during session cleanup")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage agent lifecycle on startup/shutdown."""
    import asyncio

    # Ensure managed VM backend binaries are on PATH before agent manager tries to find them
    from uniharness_api.routes.setup import ensure_managed_deps_on_path
    ensure_managed_deps_on_path()

    logger.info("Starting agent manager...")
    await agent_manager.start()
    logger.info("Agent manager started.")
    logger.info("Initialising database...")
    await init_db()
    logger.info("Database ready.")
    cleanup_task = asyncio.create_task(_cleanup_expired_sessions())
    yield
    cleanup_task.cancel()
    logger.info("Shutting down agent manager...")
    await agent_manager.stop()
    logger.info("Agent manager shut down.")


app = FastAPI(title="UniHarness API", version="0.1.0", lifespan=lifespan)

app.add_middleware(TraceContextMiddleware)

# Auth middleware (disabled by default — activated when api_key is set in config)
from uniharness_api.auth import AuthMiddleware  # noqa: E402
from uniharness_api.config import load_config  # noqa: E402

_auth = AuthMiddleware(app, api_key=load_config().api_key)
app.user_middleware.insert(0, app.user_middleware.pop())  # ensure Auth is first
# Re-register with the configured key
app.add_middleware(AuthMiddleware, api_key=load_config().api_key)

# Rate limit middleware
from uniharness_api.rate_limit import RateLimitMiddleware  # noqa: E402

app.add_middleware(RateLimitMiddleware)

# CORS — tightened from "*"; configurable via UNIHARNESS_CORS_ORIGINS env var
_cors_origins_str = os.environ.get("UNIHARNESS_CORS_ORIGINS", "")
_cors_origins = (
    [o.strip() for o in _cors_origins_str.split(",") if o.strip()]
    if _cors_origins_str
    else ["http://localhost:3000", "http://localhost:5173", "app://."]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(config.router)
app.include_router(conversations.router)
app.include_router(sessions.router)
app.include_router(setup.router)
app.include_router(skills.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness check — always returns ok if the process is running."""
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready() -> dict:
    """Readiness check — verifies database and computer availability.

    Returns HTTP 503 if any critical dependency is unavailable.
    """
    import aiosqlite

    from fastapi import Response

    from uniharness_api.paths import db_path

    status: dict = {"status": "ready", "database": "ok", "computers": {}}

    # Database connectivity
    try:
        db = await aiosqlite.connect(str(db_path()))
        await db.execute("SELECT 1")
        await db.close()
    except Exception:
        status["database"] = "unavailable"
        status["status"] = "not_ready"

    # Active computers
    for key, computer in agent_manager._computers.items():
        from uniharness.computer.base import health_check

        status["computers"][key] = "healthy" if health_check(computer) else "unhealthy"

    if status["status"] != "ready":
        return Response(content=json.dumps(status), media_type="application/json", status_code=503)

    return status
