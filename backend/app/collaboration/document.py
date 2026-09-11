"""Document manager — single-process in-memory Y.Doc store.

V1 CONSTRAINT: All WebSocket connections for a given session MUST be
handled by the same process. This manager is NOT horizontally scalable.
Horizontal scaling requires a Redis-based Y.Doc sync layer (see Milestone 3).

Uses pycrdt (the actively maintained successor to y-py) for the
Yjs-compatible CRDT implementation.
"""

import logging
import uuid

import pycrdt
from sqlalchemy import update

from app.database import async_session_factory
from app.models import Session

logger = logging.getLogger(__name__)


class DocumentManager:
    """Manages in-memory Yjs documents for active sessions."""

    def __init__(self) -> None:
        self._docs: dict[str, pycrdt.Doc] = {}

    def get_or_create(self, session_id: str) -> pycrdt.Doc:
        """Get existing Doc or create a new one for the session."""
        if session_id not in self._docs:
            doc = pycrdt.Doc()
            # Ensure the 'monaco' text type exists
            doc["monaco"] = pycrdt.Text()
            self._docs[session_id] = doc
            logger.info("Created new Y.Doc for session %s", session_id)
        return self._docs[session_id]

    async def load_from_db(self, session_id: str) -> pycrdt.Doc:
        """Load Y.Doc state from PostgreSQL if available.

        Called once when the first user connects to a session.
        """
        doc = self.get_or_create(session_id)

        async with async_session_factory() as db:
            from sqlalchemy import select as sa_select

            result = await db.execute(
                sa_select(Session.yjs_state).where(Session.id == uuid.UUID(session_id))
            )
            row = result.first()
            if row and row[0] is not None:
                try:
                    doc.apply_update(row[0])
                    logger.info(
                        "Loaded Y.Doc state from DB for session %s (%d bytes)",
                        session_id,
                        len(row[0]),
                    )
                except Exception:
                    logger.exception(
                        "Failed to apply stored Y.Doc state for session %s",
                        session_id,
                    )

        return doc

    def read_text(self, session_id: str) -> str:
        """Read the current document text as a UTF-8 string.

        Used by the execution engine to get the code to run,
        and by snapshots to serialize the current state.
        """
        doc = self._docs.get(session_id)
        if doc is None:
            return ""

        text: pycrdt.Text = doc["monaco"]
        return str(text)

    def apply_update(self, session_id: str, update_data: bytes) -> None:
        """Apply a Yjs update (from a client) to the server document."""
        doc = self.get_or_create(session_id)
        doc.apply_update(update_data)

    def get_state_vector(self, session_id: str) -> bytes:
        """Get the state vector for sync protocol step 1."""
        doc = self.get_or_create(session_id)
        return doc.get_state()

    def encode_state_as_update(
        self, session_id: str, state_vector: bytes | None = None
    ) -> bytes:
        """Encode the document state as an update.

        If state_vector is provided, returns only the diff.
        Used for sync protocol step 2.
        """
        doc = self.get_or_create(session_id)
        if state_vector is not None:
            return doc.get_update(state_vector)
        return doc.get_update()

    async def persist_to_db(self, session_id: str) -> None:
        """Save the current Y.Doc state to PostgreSQL."""
        doc = self._docs.get(session_id)
        if doc is None:
            return

        state = doc.get_update()

        async with async_session_factory() as db:
            await db.execute(
                update(Session)
                .where(Session.id == uuid.UUID(session_id))
                .values(yjs_state=state)
            )
            await db.commit()
            logger.info(
                "Persisted Y.Doc to DB for session %s (%d bytes)",
                session_id,
                len(state),
            )

    def remove_doc(self, session_id: str) -> None:
        """Remove a document from memory (e.g., when all users disconnect)."""
        if session_id in self._docs:
            del self._docs[session_id]
            logger.info("Removed Y.Doc for session %s from memory", session_id)

    @property
    def active_sessions(self) -> list[str]:
        """List session IDs with active in-memory documents."""
        return list(self._docs.keys())


# Singleton — shared across the entire process
doc_manager = DocumentManager()
