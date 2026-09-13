"""Authenticated, protocol-compatible WebSocket collaboration endpoint.

The frontend uses ``y-websocket`` v3, whose frames contain lib0 varUint
message headers. This endpoint handles the Yjs sync and awareness protocols
without treating every binary frame as a raw document update.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from weakref import WeakValueDictionary

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.auth.service import decode_token
from app.collaboration.document import doc_manager
from app.collaboration.presence import awareness_store
from app.collaboration.protocol import (
    SYNC_STEP1,
    AuthMessage,
    AwarenessEntry,
    AwarenessMessage,
    AwarenessQuery,
    ProtocolError,
    SyncMessage,
    encode_awareness_message,
    encode_document_saved,
    encode_permission_denied,
    encode_sync_step1,
    encode_sync_step2,
    encode_sync_update,
    parse_message,
)
from app.database import async_session_factory
from app.models import ROLE_RANK, RoleEnum, Session, SessionMember, User

logger = logging.getLogger(__name__)

router = APIRouter()

# Codes in 4400-4499 are permanent failures to y-websocket v3's default
# reconnect policy. Accepting before closing ensures the browser receives the
# private close code instead of an HTTP handshake rejection with code 1006.
WS_UNAUTHORIZED = 4401
WS_FORBIDDEN = 4403
WS_NOT_FOUND = 4404
WS_SESSION_CLOSED = 4410
# y-websocket treats 4400-4499 as permanent. A role change is intentionally
# transient: the client should reconnect and authenticate with its new role.
WS_ROLE_CHANGED = 4001


@dataclass(frozen=True)
class AuthorizationStamp:
    """In-memory authorization generations captured before the database read."""

    member_version: int
    session_version: int


@dataclass(frozen=True)
class AuthorizationInvalidation:
    """Latest invalidation generation and the close response it requires."""

    version: int
    code: int
    reason: str


ConnectionInfo = tuple[WebSocket, str, str, RoleEnum]
_rooms: dict[str, list[ConnectionInfo]] = {}
_room_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_member_invalidations: dict[tuple[str, str], AuthorizationInvalidation] = {}
_session_invalidations: dict[str, AuthorizationInvalidation] = {}
_connection_stamps: dict[WebSocket, AuthorizationStamp] = {}
_revoked_connections: dict[WebSocket, tuple[int, str]] = {}


class WebSocketAuthError(Exception):
    """Authentication failure carrying a permanent WebSocket close code."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


async def _authenticate_ws(
    websocket: WebSocket,
) -> tuple[User, RoleEnum, str, AuthorizationStamp]:
    """Validate the JWT, active session, and session membership."""
    token = websocket.query_params.get("token")
    raw_session_id = websocket.path_params.get("session_id")
    if not token:
        raise WebSocketAuthError(WS_UNAUTHORIZED, "Authentication required")
    if not raw_session_id:
        raise WebSocketAuthError(WS_NOT_FOUND, "Session not found")

    try:
        payload = decode_token(token)
        user_id = uuid.UUID(payload["sub"])
    except Exception as exc:
        raise WebSocketAuthError(
            WS_UNAUTHORIZED,
            "Invalid or expired access token",
        ) from exc

    try:
        session_uuid = uuid.UUID(raw_session_id)
    except ValueError as exc:
        raise WebSocketAuthError(WS_NOT_FOUND, "Session not found") from exc

    canonical_session_id = str(session_uuid)
    canonical_user_id = str(user_id)
    member_invalidation = _member_invalidations.get(
        (canonical_session_id, canonical_user_id)
    )
    session_invalidation = _session_invalidations.get(canonical_session_id)
    authorization_stamp = AuthorizationStamp(
        member_version=member_invalidation.version if member_invalidation else 0,
        session_version=session_invalidation.version if session_invalidation else 0,
    )

    async with async_session_factory() as db:
        user = await db.get(User, user_id)
        if user is None:
            raise WebSocketAuthError(WS_UNAUTHORIZED, "User not found")

        session = await db.get(Session, session_uuid)
        if session is None:
            raise WebSocketAuthError(WS_NOT_FOUND, "Session not found")
        if not session.is_active:
            raise WebSocketAuthError(WS_SESSION_CLOSED, "Session is closed")

        result = await db.execute(
            select(SessionMember).where(
                SessionMember.session_id == session_uuid,
                SessionMember.user_id == user.id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            raise WebSocketAuthError(
                WS_FORBIDDEN,
                "Session membership required",
            )

    # Canonicalizing the UUID prevents multiple in-memory rooms for equivalent
    # upper/lower-case UUID path representations.
    return user, member.role, canonical_session_id, authorization_stamp


def _authorization_failure(
    session_id: str,
    user_id: str,
    websocket: WebSocket,
) -> tuple[int, str] | None:
    """Return the invalidation that makes a cached authorization stale."""
    revoked = _revoked_connections.get(websocket)
    if revoked is not None:
        return revoked

    stamp = _connection_stamps.get(websocket)
    if stamp is None:
        return None

    session_invalidation = _session_invalidations.get(session_id)
    if (
        session_invalidation is not None
        and session_invalidation.version > stamp.session_version
    ):
        return session_invalidation.code, session_invalidation.reason

    member_invalidation = _member_invalidations.get((session_id, user_id))
    if (
        member_invalidation is not None
        and member_invalidation.version > stamp.member_version
    ):
        return member_invalidation.code, member_invalidation.reason
    return None


async def _close_connection(
    websocket: WebSocket,
    code: int,
    reason: str,
) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        # The authorization fence remains in force even when a transport has
        # already disappeared or cannot accept the close frame.
        logger.debug("Unable to close invalidated WebSocket", exc_info=True)


async def _close_if_invalidated(
    session_id: str,
    user_id: str,
    websocket: WebSocket,
) -> bool:
    failure = _authorization_failure(session_id, user_id, websocket)
    if failure is None:
        return False

    _revoked_connections[websocket] = failure
    await _close_connection(websocket, *failure)
    return True


async def invalidate_member_connections(
    session_id: str | uuid.UUID,
    user_id: str | uuid.UUID,
    *,
    code: int,
    reason: str,
    room_lock_held: bool = False,
) -> int:
    """Fence and close every cached connection for one session member."""
    canonical_session_id = str(session_id)
    canonical_user_id = str(user_id)

    def fence_connections() -> tuple[WebSocket, ...]:
        key = (canonical_session_id, canonical_user_id)
        previous = _member_invalidations.get(key)
        _member_invalidations[key] = AuthorizationInvalidation(
            version=previous.version + 1 if previous else 1,
            code=code,
            reason=reason,
        )
        targets = tuple(
            websocket
            for websocket, connected_user_id, _username, _role in _rooms.get(
                canonical_session_id, ()
            )
            if connected_user_id == canonical_user_id
        )
        for websocket in targets:
            _revoked_connections[websocket] = (code, reason)
        return targets

    if room_lock_held:
        targets = fence_connections()
    else:
        lock = _room_locks.setdefault(canonical_session_id, asyncio.Lock())
        async with lock:
            targets = fence_connections()

    await asyncio.gather(
        *(_close_connection(websocket, code, reason) for websocket in targets)
    )
    return len(targets)


async def reauthenticate_member_connections(
    session_id: str | uuid.UUID,
    user_id: str | uuid.UUID,
    *,
    room_lock_held: bool = False,
) -> int:
    """Reconnect a member so its new role is loaded from PostgreSQL."""
    return await invalidate_member_connections(
        session_id,
        user_id,
        code=WS_ROLE_CHANGED,
        reason="Session role changed; reconnecting",
        room_lock_held=room_lock_held,
    )


async def revoke_member_connections(
    session_id: str | uuid.UUID,
    user_id: str | uuid.UUID,
    *,
    room_lock_held: bool = False,
) -> int:
    """Permanently disconnect a user whose membership was removed."""
    return await invalidate_member_connections(
        session_id,
        user_id,
        code=WS_FORBIDDEN,
        reason="Session membership required",
        room_lock_held=room_lock_held,
    )


async def close_session_connections(
    session_id: str | uuid.UUID,
    *,
    room_lock_held: bool = False,
) -> int:
    """Fence and permanently close every connection for a closed session."""
    canonical_session_id = str(session_id)

    def fence_connections() -> tuple[tuple[WebSocket, ...], AuthorizationInvalidation]:
        previous = _session_invalidations.get(canonical_session_id)
        invalidation = AuthorizationInvalidation(
            version=previous.version + 1 if previous else 1,
            code=WS_SESSION_CLOSED,
            reason="Session is closed",
        )
        _session_invalidations[canonical_session_id] = invalidation
        targets = tuple(
            websocket
            for websocket, _user_id, _username, _role in _rooms.get(
                canonical_session_id, ()
            )
        )
        for websocket in targets:
            _revoked_connections[websocket] = (
                invalidation.code,
                invalidation.reason,
            )
        return targets, invalidation

    if room_lock_held:
        targets, invalidation = fence_connections()
    else:
        lock = _room_locks.setdefault(canonical_session_id, asyncio.Lock())
        async with lock:
            targets, invalidation = fence_connections()

    await asyncio.gather(
        *(
            _close_connection(
                websocket,
                invalidation.code,
                invalidation.reason,
            )
            for websocket in targets
        )
    )
    return len(targets)


async def _broadcast_binary(session_id: str, data: bytes) -> None:
    """Send a protocol frame to every current connection, including origin."""
    connections = tuple(_rooms.get(session_id, ()))
    for connection, _user_id, username, _role in connections:
        if connection in _revoked_connections:
            continue
        try:
            await connection.send_bytes(data)
        except Exception:
            # The owning receive loop performs authoritative room/presence
            # cleanup. Mutating the room here can skip last-user persistence.
            logger.debug(
                "Unable to broadcast to user=%s session=%s",
                username,
                session_id,
                exc_info=True,
            )


async def broadcast_document_update(session_id: str, update: bytes) -> None:
    """Broadcast an authorized Yjs update submitted outside the WS loop."""
    await _broadcast_binary(session_id, encode_sync_update(update))


def _document_saved_frame(
    *,
    saved_at: datetime | None,
    state_vector: bytes,
    state_hash: str,
    dirty: bool,
    saved_by: str | None,
) -> bytes:
    return encode_document_saved(
        saved_at=saved_at.isoformat() if saved_at is not None else None,
        state_vector=base64.b64encode(state_vector).decode("ascii"),
        state_hash=state_hash,
        dirty=dirty,
        saved_by=saved_by,
    )


async def broadcast_document_saved(
    session_id: str,
    *,
    saved_at: datetime,
    state_vector: bytes,
    state_hash: str,
    dirty: bool,
    saved_by: str,
) -> None:
    """Tell every peer which exact CRDT state reached PostgreSQL."""
    await _broadcast_binary(
        session_id,
        _document_saved_frame(
            saved_at=saved_at,
            state_vector=state_vector,
            state_hash=state_hash,
            dirty=dirty,
            saved_by=saved_by,
        ),
    )


async def _send_current_document_status(
    websocket: WebSocket,
    session_id: str,
) -> None:
    status = doc_manager.get_document_status(session_id)
    if status is None:
        return
    await websocket.send_bytes(
        _document_saved_frame(
            saved_at=status.saved_at,
            state_vector=status.saved_state_vector,
            state_hash=status.saved_state_hash,
            dirty=status.dirty,
            saved_by=None,
        )
    )


@asynccontextmanager
async def document_room_lifecycle(session_id: str) -> AsyncIterator[bool]:
    """Hold joins/leaves stable while an HTTP save uses the shared document.

    The yielded value reports whether a WebSocket room is active. A document
    loaded only for HTTP can then be checkpointed and evicted without racing a
    first connection that is still joining.
    """
    lock = _room_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        yield bool(_rooms.get(session_id))


async def maintain_document_checkpoint(session_id: str) -> None:
    """Checkpoint active rooms or retry durable eviction for an orphan doc."""
    lock = _room_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        if not _rooms.get(session_id):
            removed = await doc_manager.checkpoint_and_remove(session_id)
            if not removed:
                logger.info(
                    "Retained orphan Y.Doc for another checkpoint retry: session=%s",
                    session_id,
                )
            return

    # Active-room edits need not wait for Redis I/O. Generation tracking makes
    # a concurrent edit visible to the next periodic checkpoint.
    await doc_manager.checkpoint_to_redis(session_id)


async def _broadcast_awareness(
    session_id: str,
    entries: tuple[AwarenessEntry, ...],
) -> None:
    if entries:
        await _broadcast_binary(
            session_id,
            encode_awareness_message(entries),
        )


async def _prune_stale_awareness(session_id: str) -> None:
    removed = awareness_store.prune_stale(session_id)
    await _broadcast_awareness(session_id, removed)


async def _handle_binary_message(
    websocket: WebSocket,
    session_id: str,
    role: RoleEnum,
    username: str,
    data: bytes,
    *,
    user_id: str | None = None,
) -> None:
    """Validate and process one complete y-websocket binary frame."""
    if websocket in _revoked_connections:
        return

    try:
        message = parse_message(data)
    except ProtocolError as exc:
        logger.warning(
            "Ignoring malformed collaboration frame from user=%s session=%s: %s",
            username,
            session_id,
            exc,
        )
        return

    if isinstance(message, SyncMessage):
        if message.subtype == SYNC_STEP1:
            try:
                update = doc_manager.encode_state_as_update(
                    session_id,
                    message.payload,
                )
            except Exception:
                logger.warning(
                    "Ignoring invalid state vector from user=%s session=%s",
                    username,
                    session_id,
                    exc_info=True,
                )
                return
            await websocket.send_bytes(encode_sync_step2(update))
            return

        failure: tuple[int, str] | None = None
        denied = False
        applied = False
        close_required = False

        def apply_if_authorized() -> None:
            nonlocal applied, close_required, denied, failure
            if user_id is not None:
                failure = _authorization_failure(session_id, user_id, websocket)
                if failure is not None:
                    close_required = websocket not in _revoked_connections
                    _revoked_connections[websocket] = failure
                    return
            if ROLE_RANK[role] < ROLE_RANK[RoleEnum.editor]:
                denied = True
                return
            try:
                doc_manager.apply_update(session_id, message.payload)
            except Exception:
                logger.warning(
                    "Ignoring invalid Yjs update from user=%s session=%s",
                    username,
                    session_id,
                    exc_info=True,
                )
                return
            applied = True

        if user_id is None:
            apply_if_authorized()
        else:
            lock = _room_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                apply_if_authorized()

        if failure is not None:
            if close_required:
                await _close_connection(websocket, *failure)
            return
        if denied:
            await websocket.send_bytes(
                encode_permission_denied("Viewers cannot edit the document")
            )
            return
        if not applied:
            return

        # Sync step 2 is a targeted handshake response. Once applied to the
        # authoritative document, normalize it to the ordinary update subtype
        # before broadcasting so unrelated clients are not marked as synced.
        await _broadcast_binary(
            session_id,
            encode_sync_update(message.payload),
        )
        return

    if isinstance(message, AwarenessMessage):
        accepted = awareness_store.apply_update(
            session_id,
            websocket,
            message.entries,
        )
        # Awareness is intentionally echoed to its origin. y-websocket uses the
        # echoed 15-second heartbeat to detect a healthy connection.
        await _broadcast_awareness(session_id, accepted)
        return

    if isinstance(message, AwarenessQuery):
        await websocket.send_bytes(
            encode_awareness_message(awareness_store.active_entries(session_id))
        )
        return

    if isinstance(message, AuthMessage):
        logger.warning(
            "Ignoring unexpected client auth frame from user=%s session=%s",
            username,
            session_id,
        )


async def _join_room(
    session_id: str,
    connection: ConnectionInfo,
) -> None:
    lock = _room_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        room = _rooms.setdefault(session_id, [])
        if not room:
            await doc_manager.load_from_storage(session_id)
        room.append(connection)


async def _leave_room(session_id: str, websocket: WebSocket) -> None:
    """Remove a connection and checkpoint the last in-memory state."""
    lock = _room_locks.setdefault(session_id, asyncio.Lock())
    removed_awareness: tuple[AwarenessEntry, ...] = ()
    is_last = False

    async with lock:
        _connection_stamps.pop(websocket, None)
        _revoked_connections.pop(websocket, None)
        room = _rooms.get(session_id)
        if room is None:
            return

        _rooms[session_id] = [
            connection for connection in room if connection[0] is not websocket
        ]
        removed_awareness = awareness_store.remove_owner(session_id, websocket)
        is_last = not _rooms[session_id]

        if is_last:
            _rooms.pop(session_id, None)
            try:
                await doc_manager.checkpoint_and_remove(session_id)
            except Exception:
                # Retain the in-memory document if checkpointing fails so a
                # later reconnect or periodic flush can recover it.
                logger.exception(
                    "Failed to checkpoint collaboration state for session %s",
                    session_id,
                )
            awareness_store.discard_session(session_id)

    if not is_last:
        await _broadcast_awareness(session_id, removed_awareness)


@router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str) -> None:
    """Synchronize one authenticated member with a collaborative Y.Doc."""
    try:
        user, role, session_id, authorization_stamp = await _authenticate_ws(websocket)
    except WebSocketAuthError as exc:
        await websocket.accept()
        await websocket.close(code=exc.code, reason=exc.reason)
        return

    await websocket.accept()
    user_id = str(user.id)
    username = user.username
    connection: ConnectionInfo = (websocket, user_id, username, role)
    joined = False
    _connection_stamps[websocket] = authorization_stamp

    try:
        if await _close_if_invalidated(session_id, user_id, websocket):
            return
        await _join_room(session_id, connection)
        joined = True
        if await _close_if_invalidated(session_id, user_id, websocket):
            return
        logger.info(
            "WS connected: user=%s session=%s role=%s",
            username,
            session_id,
            role.value,
        )

        # Editors participate in the bidirectional handshake so pending local
        # edits can merge into the authoritative document. A viewer only sends
        # its normal client step 1 and receives server state; asking it for step
        # 2 would make a legitimate read-only connection look like a write.
        if ROLE_RANK[role] >= ROLE_RANK[RoleEnum.editor]:
            await websocket.send_bytes(
                encode_sync_step1(doc_manager.get_state_vector(session_id))
            )
        await _send_current_document_status(websocket, session_id)
        active_awareness = awareness_store.active_entries(session_id)
        if active_awareness:
            await websocket.send_bytes(encode_awareness_message(active_awareness))

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            if await _close_if_invalidated(session_id, user_id, websocket):
                break

            await _prune_stale_awareness(session_id)

            binary = message.get("bytes")
            if binary is not None:
                await _handle_binary_message(
                    websocket,
                    session_id,
                    role,
                    username,
                    binary,
                    user_id=user_id,
                )
                continue

            # y-websocket itself is binary-only. Text frames are reserved for
            # later execution-output features and cannot be fed to its decoder.
            if message.get("text") is not None:
                logger.debug(
                    "Ignoring unsupported text frame from user=%s session=%s",
                    username,
                    session_id,
                )

    except WebSocketDisconnect:
        logger.info("WS disconnected: user=%s session=%s", username, session_id)
    except Exception:
        logger.exception("WS error: user=%s session=%s", username, session_id)
        with suppress(Exception):
            await websocket.close(code=1011, reason="Collaboration server error")
    finally:
        if joined:
            await _leave_room(session_id, websocket)
        else:
            _connection_stamps.pop(websocket, None)
            _revoked_connections.pop(websocket, None)


def get_room_connections(session_id: str) -> list[ConnectionInfo]:
    """Return a copy of active connections for the execution module."""
    return [
        connection
        for connection in _rooms.get(session_id, ())
        if connection[0] not in _revoked_connections
    ]
