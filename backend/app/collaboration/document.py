"""Single-process Yjs document storage and durability boundaries.

The in-memory document is authoritative while the backend is running. Redis
holds recovery checkpoints for process crashes, while PostgreSQL is updated
only by an explicit save. Horizontal collaboration still requires one backend
process; Redis checkpoints are not a cross-process synchronization protocol.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import pycrdt
from redis.asyncio import Redis
from sqlalchemy import select, update

from app.config import settings
from app.database import async_session_factory
from app.models import Session

logger = logging.getLogger(__name__)


class RedisDocumentClient(Protocol):
    """Subset of the async Redis client used by the document manager."""

    async def get(self, key: str) -> bytes | None: ...

    async def set(
        self,
        key: str,
        value: bytes,
        *,
        ex: int | None = None,
    ) -> object: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class DocumentSaveResult:
    """Metadata returned after an explicit PostgreSQL save."""

    size_bytes: int
    saved_at: datetime
    state_vector: bytes
    state_hash: str
    dirty: bool


@dataclass(frozen=True)
class DocumentStatus:
    """Server-authoritative durability status for one active document."""

    saved_state_vector: bytes
    saved_state_hash: str
    saved_at: datetime | None
    dirty: bool
    generation: int
    checkpointed_generation: int | None


@dataclass(frozen=True)
class _StoredDocument:
    state: bytes | None
    saved_at: datetime | None


@dataclass
class _DocumentMetadata:
    generation: int
    checkpointed_generation: int | None
    saved_state_vector: bytes
    saved_state_hash: str
    saved_at: datetime | None


@dataclass
class _SessionGuard:
    lock: asyncio.Lock
    users: int = 0


class DocumentManager:
    """Manage live Yjs documents, recovery checkpoints, and explicit saves."""

    def __init__(
        self,
        redis_client: RedisDocumentClient | None = None,
        *,
        checkpoint_ttl_seconds: int | None = None,
    ) -> None:
        self._docs: dict[str, pycrdt.Doc] = {}
        self._metadata: dict[str, _DocumentMetadata] = {}
        self._guards: dict[str, _SessionGuard] = {}
        self._loading_sessions: set[str] = set()
        self._checkpoint_ttl_seconds = (
            checkpoint_ttl_seconds
            if checkpoint_ttl_seconds is not None
            else getattr(settings, "crdt_checkpoint_ttl_seconds", 7 * 24 * 60 * 60)
        )
        self._redis: RedisDocumentClient = redis_client or Redis.from_url(
            settings.redis_url,
            decode_responses=False,
            socket_connect_timeout=5.0,
            socket_timeout=5.0,
            health_check_interval=30,
        )

    @staticmethod
    def checkpoint_key(session_id: str) -> str:
        """Return the stable Redis key for one session's recovery state."""
        return f"concord:document:{session_id}:checkpoint"

    @staticmethod
    def _new_doc() -> pycrdt.Doc:
        doc = pycrdt.Doc()
        doc["monaco"] = pycrdt.Text()
        return doc

    @staticmethod
    def _state_hash(state: bytes) -> str:
        """Return a deletion-aware digest of a canonical full Yjs update."""
        return hashlib.sha256(state).hexdigest()

    @asynccontextmanager
    async def _serialized(self, session_id: str) -> AsyncIterator[None]:
        """Serialize storage operations for a session and retire idle guards."""
        guard = self._guards.setdefault(session_id, _SessionGuard(asyncio.Lock()))
        guard.users += 1
        try:
            async with guard.lock:
                yield
        finally:
            guard.users -= 1
            if (
                guard.users == 0
                and session_id not in self._docs
                and self._guards.get(session_id) is guard
            ):
                self._guards.pop(session_id, None)

    def _publish_new_doc(self, session_id: str, doc: pycrdt.Doc) -> None:
        state_vector = doc.get_state()
        state_hash = self._state_hash(doc.get_update())
        self._docs[session_id] = doc
        self._metadata[session_id] = _DocumentMetadata(
            generation=0,
            checkpointed_generation=None,
            saved_state_vector=state_vector,
            saved_state_hash=state_hash,
            saved_at=None,
        )
        logger.info("Created new Y.Doc for session %s", session_id)

    def get_or_create(self, session_id: str) -> pycrdt.Doc:
        """Get an existing document or create an empty one for the session."""
        doc = self._docs.get(session_id)
        if doc is not None:
            return doc
        if session_id in self._loading_sessions:
            raise RuntimeError(f"Document for session {session_id} is still loading")

        doc = self._new_doc()
        self._publish_new_doc(session_id, doc)
        return doc

    async def _load_db_state(self, session_id: str) -> _StoredDocument:
        async with async_session_factory() as db:
            result = await db.execute(
                select(Session.yjs_state, Session.updated_at).where(
                    Session.id == uuid.UUID(session_id)
                )
            )
            row = result.first()
            if row is None:
                return _StoredDocument(state=None, saved_at=None)
            return _StoredDocument(state=row[0], saved_at=row[1])

    async def _load_from_storage_locked(self, session_id: str) -> pycrdt.Doc:
        existing = self._docs.get(session_id)
        if existing is not None:
            return existing

        doc = self._new_doc()
        self._loading_sessions.add(session_id)
        try:
            stored = await self._load_db_state(session_id)
            # Keep compatibility with tests and integrations that replace the
            # pre-checkpoint loader with its former bytes-only return value.
            if isinstance(stored, bytes) or stored is None:
                db_state = stored
                db_saved_at = None
            else:
                db_state = stored.state
                db_saved_at = stored.saved_at

            if db_state is not None:
                try:
                    doc.apply_update(db_state)
                    logger.info(
                        "Loaded saved Y.Doc state for session %s (%d bytes)",
                        session_id,
                        len(db_state),
                    )
                except Exception:
                    logger.exception(
                        "Failed to apply saved Y.Doc state for session %s",
                        session_id,
                    )

            # Record the explicit-save boundary before applying recovery data.
            saved_state_vector = doc.get_state()
            saved_state_hash = self._state_hash(doc.get_update())

            try:
                checkpoint = await self._redis.get(self.checkpoint_key(session_id))
            except Exception:
                # Redis recovery is best-effort. A Redis outage must not prevent
                # opening the last explicitly saved database version.
                logger.exception(
                    "Failed to read Redis checkpoint for session %s",
                    session_id,
                )
                checkpoint = None

            recovered_checkpoint = False
            if checkpoint is not None:
                try:
                    doc.apply_update(checkpoint)
                    recovered_checkpoint = True
                    logger.info(
                        "Recovered Y.Doc checkpoint for session %s (%d bytes)",
                        session_id,
                        len(checkpoint),
                    )
                except Exception:
                    logger.exception(
                        "Failed to apply Redis checkpoint for session %s",
                        session_id,
                    )

            # Publish only after both sources have finished. This prevents the
            # periodic task and concurrent callers from seeing a partial load.
            self._docs[session_id] = doc
            self._metadata[session_id] = _DocumentMetadata(
                generation=0,
                checkpointed_generation=0 if recovered_checkpoint else None,
                saved_state_vector=saved_state_vector,
                saved_state_hash=saved_state_hash,
                saved_at=db_saved_at if db_state is not None else None,
            )
            return doc
        finally:
            self._loading_sessions.discard(session_id)

    async def load_from_storage(self, session_id: str) -> pycrdt.Doc:
        """Load the database save and merge a newer Redis checkpoint once.

        Applying both updates is intentional: Yjs updates are idempotent and
        commutative. PostgreSQL supplies the last explicit save and Redis can
        add newer unsaved edits recovered after a process restart.
        """
        async with self._serialized(session_id):
            return await self._load_from_storage_locked(session_id)

    # Compatibility for callers and tests written against the Milestone 1 API.
    load_from_db = load_from_storage

    def read_text(self, session_id: str) -> str:
        """Read the current document text as a UTF-8 string."""
        doc = self._docs.get(session_id)
        if doc is None:
            return ""

        text: pycrdt.Text = doc["monaco"]
        return str(text)

    def apply_update(self, session_id: str, update_data: bytes) -> None:
        """Apply a Yjs update from an authorized client and mark new state."""
        doc = self.get_or_create(session_id)
        doc.apply_update(update_data)
        # Delete-only Yjs updates do not advance the state vector. Incrementing
        # after every valid update conservatively protects checkpoint eviction;
        # duplicate updates may cause an extra retry but can never lose state.
        self._metadata[session_id].generation += 1

    def get_state_vector(self, session_id: str) -> bytes:
        """Get the state vector for sync protocol step 1."""
        return self.get_or_create(session_id).get_state()

    def encode_state_as_update(
        self, session_id: str, state_vector: bytes | None = None
    ) -> bytes:
        """Encode all state, or only the diff from a supplied state vector."""
        doc = self.get_or_create(session_id)
        if state_vector is not None:
            return doc.get_update(state_vector)
        return doc.get_update()

    def get_document_status(self, session_id: str) -> DocumentStatus | None:
        """Return the current DB-save and Redis-checkpoint boundaries."""
        doc = self._docs.get(session_id)
        metadata = self._metadata.get(session_id)
        if doc is None or metadata is None:
            return None
        return DocumentStatus(
            saved_state_vector=metadata.saved_state_vector,
            saved_state_hash=metadata.saved_state_hash,
            saved_at=metadata.saved_at,
            dirty=self._state_hash(doc.get_update()) != metadata.saved_state_hash,
            generation=metadata.generation,
            checkpointed_generation=metadata.checkpointed_generation,
        )

    async def _checkpoint_to_redis_locked(self, session_id: str) -> int:
        doc = self._docs.get(session_id)
        metadata = self._metadata.get(session_id)
        if doc is None or metadata is None:
            return 0

        state = doc.get_update()
        generation = metadata.generation
        await self._redis.set(
            self.checkpoint_key(session_id),
            state,
            ex=self._checkpoint_ttl_seconds,
        )
        # An update can be applied while Redis I/O is in flight. Record the
        # generation actually written so a later checkpoint knows to catch up.
        metadata.checkpointed_generation = generation
        logger.info(
            "Checkpointed Y.Doc to Redis for session %s (%d bytes, generation=%d)",
            session_id,
            len(state),
            generation,
        )
        return len(state)

    async def checkpoint_to_redis(self, session_id: str) -> int:
        """Write the current full Yjs state to the Redis recovery store."""
        async with self._serialized(session_id):
            return await self._checkpoint_to_redis_locked(session_id)

    async def _persist_to_db_locked(self, session_id: str) -> DocumentSaveResult:
        doc = self._docs.get(session_id)
        if doc is None:
            doc = await self._load_from_storage_locked(session_id)

        state = doc.get_update()
        state_vector = doc.get_state()
        state_hash = self._state_hash(state)
        saved_at = datetime.now(UTC)

        async with async_session_factory() as db:
            await db.execute(
                update(Session)
                .where(Session.id == uuid.UUID(session_id))
                .values(yjs_state=state, updated_at=saved_at)
            )
            await db.commit()

        metadata = self._metadata[session_id]
        metadata.saved_state_vector = state_vector
        metadata.saved_state_hash = state_hash
        metadata.saved_at = saved_at
        dirty = self._state_hash(doc.get_update()) != state_hash
        logger.info(
            "Explicitly saved Y.Doc to DB for session %s (%d bytes)",
            session_id,
            len(state),
        )
        return DocumentSaveResult(
            size_bytes=len(state),
            saved_at=saved_at,
            state_vector=state_vector,
            state_hash=state_hash,
            dirty=dirty,
        )

    async def persist_to_db(self, session_id: str) -> DocumentSaveResult:
        """Explicitly save the current full Yjs state to PostgreSQL."""
        async with self._serialized(session_id):
            return await self._persist_to_db_locked(session_id)

    async def checkpoint_and_remove(self, session_id: str) -> bool:
        """Checkpoint and remove an unchanged document as one serialized step.

        Returns False when an edit arrives during Redis I/O. In that case the
        in-memory document is retained so the newer generation cannot be lost
        and a later periodic checkpoint can retry it.
        """
        async with self._serialized(session_id):
            doc = self._docs.get(session_id)
            metadata = self._metadata.get(session_id)
            if doc is None or metadata is None:
                return True

            generation = metadata.generation
            await self._checkpoint_to_redis_locked(session_id)
            if metadata.generation != generation:
                logger.info(
                    "Retaining Y.Doc for session %s after a concurrent edit",
                    session_id,
                )
                return False

            self._docs.pop(session_id, None)
            self._metadata.pop(session_id, None)
            logger.info("Removed Y.Doc for session %s from memory", session_id)
            return True

    def remove_doc(self, session_id: str) -> bool:
        """Remove a fully checkpointed document when no storage work is queued.

        New code should prefer checkpoint_and_remove. This compatibility method
        refuses unsafe removal rather than discarding a newer generation.
        """
        metadata = self._metadata.get(session_id)
        guard = self._guards.get(session_id)
        if metadata is None:
            return True
        if metadata.checkpointed_generation != metadata.generation:
            return False
        if guard is not None and guard.users:
            return False

        self._docs.pop(session_id, None)
        self._metadata.pop(session_id, None)
        if guard is not None and self._guards.get(session_id) is guard:
            self._guards.pop(session_id, None)
        logger.info("Removed Y.Doc for session %s from memory", session_id)
        return True

    async def close(self) -> None:
        """Close the Redis connection owned by the manager."""
        await self._redis.aclose()

    @property
    def active_sessions(self) -> list[str]:
        """List fully loaded session IDs with active in-memory documents."""
        return list(self._docs.keys())


doc_manager = DocumentManager()
