"""Live authorization invalidation for connected collaboration clients."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.collaboration import websocket as websocket_module
from app.collaboration.protocol import SYNC_UPDATE, encode_sync_message
from app.models import RoleEnum
from app.sessions import router as session_router
from app.sessions.schemas import UpdateMemberRoleRequest


class _FakeWebSocket:
    def __init__(self, *, close_fails: bool = False) -> None:
        self.close_fails = close_fails
        self.closed: list[tuple[int, str]] = []
        self.sent: list[bytes] = []

    async def close(self, *, code: int, reason: str) -> None:
        self.closed.append((code, reason))
        if self.close_fails:
            raise RuntimeError("transport already gone")

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)


class _FakeDocumentManager:
    def __init__(self) -> None:
        self.applied: list[tuple[str, bytes]] = []

    def apply_update(self, session_id: str, update: bytes) -> None:
        self.applied.append((session_id, update))


def _reset_connection_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(websocket_module, "_rooms", {})
    monkeypatch.setattr(websocket_module, "_room_locks", {})
    monkeypatch.setattr(websocket_module, "_member_invalidations", {})
    monkeypatch.setattr(websocket_module, "_session_invalidations", {})
    monkeypatch.setattr(websocket_module, "_connection_stamps", {})
    monkeypatch.setattr(websocket_module, "_revoked_connections", {})


@pytest.mark.asyncio
async def test_role_change_fences_only_target_member_and_allows_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_connection_state(monkeypatch)
    target = _FakeWebSocket()
    peer = _FakeWebSocket()
    manager = _FakeDocumentManager()
    monkeypatch.setattr(websocket_module, "doc_manager", manager)
    websocket_module._rooms["room"] = [
        (target, "user-1", "ada", RoleEnum.editor),
        (peer, "user-2", "grace", RoleEnum.editor),
    ]

    count = await websocket_module.reauthenticate_member_connections("room", "user-1")
    await websocket_module._handle_binary_message(
        target,
        "room",
        RoleEnum.editor,
        "ada",
        encode_sync_message(SYNC_UPDATE, b"stale-editor-update"),
    )
    await websocket_module._broadcast_binary("room", b"next-update")

    assert count == 1
    assert target.closed == [
        (
            websocket_module.WS_ROLE_CHANGED,
            "Session role changed; reconnecting",
        )
    ]
    assert websocket_module.WS_ROLE_CHANGED < 4400
    assert manager.applied == []
    assert target.sent == []
    assert peer.sent == [b"next-update"]


@pytest.mark.asyncio
async def test_removed_member_cannot_apply_frames_even_if_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_connection_state(monkeypatch)
    target = _FakeWebSocket(close_fails=True)
    manager = _FakeDocumentManager()
    websocket_module._rooms["room"] = [(target, "user-1", "ada", RoleEnum.editor)]
    monkeypatch.setattr(websocket_module, "doc_manager", manager)

    await websocket_module.revoke_member_connections("room", "user-1")
    await websocket_module._handle_binary_message(
        target,
        "room",
        RoleEnum.editor,
        "ada",
        encode_sync_message(SYNC_UPDATE, b"forbidden-update"),
    )

    assert target.closed == [
        (websocket_module.WS_FORBIDDEN, "Session membership required")
    ]
    assert manager.applied == []
    assert websocket_module.get_room_connections("room") == []


@pytest.mark.asyncio
async def test_closed_session_fences_every_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_connection_state(monkeypatch)
    first = _FakeWebSocket()
    second = _FakeWebSocket()
    websocket_module._rooms["room"] = [
        (first, "user-1", "ada", RoleEnum.owner),
        (second, "user-2", "grace", RoleEnum.viewer),
    ]

    count = await websocket_module.close_session_connections("room")
    await websocket_module._broadcast_binary("room", b"hidden")

    expected = [(websocket_module.WS_SESSION_CLOSED, "Session is closed")]
    assert count == 2
    assert first.closed == expected
    assert second.closed == expected
    assert first.sent == []
    assert second.sent == []


@pytest.mark.asyncio
async def test_stale_prejoin_authorization_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_connection_state(monkeypatch)
    socket = _FakeWebSocket()
    websocket_module._connection_stamps[socket] = websocket_module.AuthorizationStamp(
        member_version=0,
        session_version=0,
    )

    await websocket_module.revoke_member_connections("room", "user-1")

    assert await websocket_module._close_if_invalidated("room", "user-1", socket)
    assert socket.closed == [
        (websocket_module.WS_FORBIDDEN, "Session membership required")
    ]


@pytest.mark.asyncio
async def test_queued_update_rechecks_invalidation_inside_session_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_connection_state(monkeypatch)
    socket = _FakeWebSocket()
    manager = _FakeDocumentManager()
    monkeypatch.setattr(websocket_module, "doc_manager", manager)
    websocket_module._rooms["room"] = [(socket, "user-1", "ada", RoleEnum.editor)]
    websocket_module._connection_stamps[socket] = websocket_module.AuthorizationStamp(
        member_version=0,
        session_version=0,
    )

    async with websocket_module.document_room_lifecycle("room"):
        update_task = asyncio.create_task(
            websocket_module._handle_binary_message(
                socket,
                "room",
                RoleEnum.editor,
                "ada",
                encode_sync_message(SYNC_UPDATE, b"stale-update"),
                user_id="user-1",
            )
        )
        await asyncio.sleep(0)
        assert manager.applied == []
        await websocket_module.revoke_member_connections(
            "room",
            "user-1",
            room_lock_held=True,
        )

    await update_task

    assert manager.applied == []
    assert socket.closed == [
        (websocket_module.WS_FORBIDDEN, "Session membership required")
    ]


@pytest.mark.asyncio
async def test_mutation_endpoints_invalidate_only_after_service_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    events: list[str] = []
    _reset_connection_state(monkeypatch)

    def assert_gate(kwargs: dict) -> None:
        assert kwargs["room_lock_held"] is True
        assert websocket_module._room_locks[str(session_id)].locked()

    async def update_service(*_args, **_kwargs):
        events.append("role committed")
        return SimpleNamespace(role=RoleEnum.viewer)

    async def reauthenticate(*_args, **_kwargs):
        assert_gate(_kwargs)
        events.append("role invalidated")

    async def remove_service(*_args, **_kwargs):
        events.append("removal committed")

    async def revoke(*_args, **_kwargs):
        assert_gate(_kwargs)
        events.append("membership invalidated")

    async def close_service(*_args, **_kwargs):
        events.append("close committed")

    async def disconnect_session(*_args, **_kwargs):
        assert_gate(_kwargs)
        events.append("session invalidated")

    monkeypatch.setattr(session_router, "update_member_role", update_service)
    monkeypatch.setattr(session_router, "remove_member", remove_service)
    monkeypatch.setattr(session_router, "close_session", close_service)
    monkeypatch.setattr(
        websocket_module,
        "reauthenticate_member_connections",
        reauthenticate,
    )
    monkeypatch.setattr(websocket_module, "revoke_member_connections", revoke)
    monkeypatch.setattr(
        websocket_module,
        "close_session_connections",
        disconnect_session,
    )

    db = MagicMock()
    current_user = SimpleNamespace(id=uuid.uuid4())
    await session_router.update_member_role_endpoint(
        session_id,
        user_id,
        UpdateMemberRoleRequest(role=RoleEnum.viewer),
        current_user,
        db,
    )
    await session_router.remove_member_endpoint(
        session_id,
        user_id,
        current_user,
        db,
    )
    await session_router.close_session_endpoint(session_id, current_user, db)

    assert events == [
        "role committed",
        "role invalidated",
        "removal committed",
        "membership invalidated",
        "close committed",
        "session invalidated",
    ]


@pytest.mark.asyncio
async def test_failed_mutation_does_not_invalidate_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    invalidation = AsyncMock()

    async def failed_update(*_args, **_kwargs):
        raise session_router.SessionConflictError("conflict")

    monkeypatch.setattr(session_router, "update_member_role", failed_update)
    monkeypatch.setattr(
        websocket_module,
        "reauthenticate_member_connections",
        invalidation,
    )

    with pytest.raises(HTTPException):
        await session_router.update_member_role_endpoint(
            session_id,
            user_id,
            UpdateMemberRoleRequest(role=RoleEnum.viewer),
            SimpleNamespace(id=uuid.uuid4()),
            MagicMock(),
        )

    invalidation.assert_not_awaited()


@pytest.mark.asyncio
async def test_orphan_checkpoint_cleanup_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_connection_state(monkeypatch)
    manager = SimpleNamespace(
        checkpoint_and_remove=AsyncMock(
            side_effect=[ConnectionError("redis unavailable"), True]
        ),
        checkpoint_to_redis=AsyncMock(),
    )
    monkeypatch.setattr(websocket_module, "doc_manager", manager)

    with pytest.raises(ConnectionError, match="redis unavailable"):
        await websocket_module.maintain_document_checkpoint("room")
    await websocket_module.maintain_document_checkpoint("room")

    assert manager.checkpoint_and_remove.await_count == 2
    manager.checkpoint_to_redis.assert_not_awaited()
