"""Concord — FastAPI application entrypoint.

Assembles routers, configures CORS and lifespan events (DB init,
periodic CRDT flush, Redis connection).
"""

import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth.router import router as auth_router
from app.collaboration.document import doc_manager
from app.collaboration.websocket import maintain_document_checkpoint
from app.collaboration.websocket import router as ws_router
from app.config import settings
from app.sessions.router import router as sessions_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

_TOKEN_QUERY_PATTERN = re.compile(r"([?&]token=)[^&\s\"]+")


class _RedactTokenQueryFilter(logging.Filter):
    """Prevent WebSocket query-string credentials from reaching logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_QUERY_PATTERN.sub(r"\1[REDACTED]", value)
                if isinstance(value, str)
                else value
                for value in record.args
            )
        return True


for logger_name in ("uvicorn.error", "uvicorn.access"):
    logging.getLogger(logger_name).addFilter(_RedactTokenQueryFilter())


async def _periodic_crdt_flush(stop_event: asyncio.Event):
    """Background task: checkpoint all active Y.Docs to Redis periodically."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=settings.crdt_flush_interval_seconds,
            )
        except TimeoutError:
            pass
        if stop_event.is_set():
            break
        for session_id in doc_manager.active_sessions:
            try:
                await maintain_document_checkpoint(session_id)
            except Exception:
                logger.exception(
                    "Failed periodic CRDT checkpoint for session %s", session_id
                )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — startup and shutdown hooks."""
    logger.info("Concord starting up…")

    # Start periodic CRDT flush
    flush_stop = asyncio.Event()
    flush_task = asyncio.create_task(_periodic_crdt_flush(flush_stop))
    logger.info(
        "CRDT flush task started (interval=%ds)",
        settings.crdt_flush_interval_seconds,
    )

    try:
        yield
    finally:
        # Shutdown: checkpoint active documents without changing explicit saves.
        # The finally block also runs when application lifespan exits because of
        # an exception, so Redis is not leaked and recovery gets a final chance.
        logger.info("Shutting down — checkpointing active documents…")
        flush_stop.set()
        try:
            # Drain an in-flight Redis write before issuing the final snapshot;
            # cancelling it would make server-side completion order ambiguous.
            await flush_task
        finally:
            try:
                for session_id in doc_manager.active_sessions:
                    try:
                        await doc_manager.checkpoint_to_redis(session_id)
                    except Exception:
                        logger.exception(
                            "Failed to checkpoint session %s on shutdown",
                            session_id,
                        )
            finally:
                await doc_manager.close()
        logger.info("Concord shut down cleanly.")


app = FastAPI(
    title="Concord",
    description="Collaborative Code Execution Platform",
    version="0.1.0",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(sessions_router)
app.include_router(ws_router)


@app.get("/health")
async def health():
    """Health check endpoint for monitoring and load balancers."""
    return {
        "status": "ok",
        "active_sessions": len(doc_manager.active_sessions),
    }
