"""Authenticated, protocol-compatible WebSocket collaboration endpoint.

The frontend uses ``y-websocket`` v3, whose frames contain lib0 varUint
message headers. This endpoint handles the Yjs sync and awareness protocols
without treating every binary frame as a raw document update.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress

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

ConnectionInfo = tuple[WebSocket, str, str, RoleEnum]
_rooms: dict[str, list[ConnectionInfo]] = {}
_room_locks: dict[str, asyncio.Lock] = {}


class WebSocketAuthError(Exception):
    """Authentication failure carrying a permanent WebSocket close code."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


async def _authenticate_ws(websocket: WebSocket) -> tuple[User, RoleEnum, str]:
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
    return user, member.role, str(session_uuid)


async def _broadcast_binary(session_id: str, data: bytes) -> None:
    """Send a protocol frame to every current connection, including origin."""
    connections = tuple(_rooms.get(session_id, ()))
    for connection, _user_id, username, _role in connections:
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
) -> None:
    """Validate and process one complete y-websocket binary frame."""
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

        if message.modifies_document and ROLE_RANK[role] < ROLE_RANK[RoleEnum.editor]:
            await websocket.send_bytes(
                encode_permission_denied("Viewers cannot edit the document")
            )
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
            await doc_manager.load_from_db(session_id)
        room.append(connection)


async def _leave_room(session_id: str, websocket: WebSocket) -> None:
    """Remove a connection, announce presence removal, and persist last state."""
    lock = _room_locks.setdefault(session_id, asyncio.Lock())
    removed_awareness: tuple[AwarenessEntry, ...] = ()
    is_last = False

    async with lock:
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
                await doc_manager.persist_to_db(session_id)
            except Exception:
                # Retain the in-memory document if persistence fails so a later
                # reconnect or periodic flush can recover it.
                logger.exception(
                    "Failed to persist collaboration state for session %s",
                    session_id,
                )
            else:
                doc_manager.remove_doc(session_id)
            awareness_store.discard_session(session_id)

    if not is_last:
        await _broadcast_awareness(session_id, removed_awareness)


@router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str) -> None:
    """Synchronize one authenticated member with a collaborative Y.Doc."""
    try:
        user, role, session_id = await _authenticate_ws(websocket)
    except WebSocketAuthError as exc:
        await websocket.accept()
        await websocket.close(code=exc.code, reason=exc.reason)
        return

    await websocket.accept()
    user_id = str(user.id)
    username = user.username
    connection: ConnectionInfo = (websocket, user_id, username, role)
    joined = False

    try:
        await _join_room(session_id, connection)
        joined = True
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
        active_awareness = awareness_store.active_entries(session_id)
        if active_awareness:
            await websocket.send_bytes(encode_awareness_message(active_awareness))

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
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


def get_room_connections(session_id: str) -> list[ConnectionInfo]:
    """Return a copy of active connections for the execution module."""
    return list(_rooms.get(session_id, ()))
