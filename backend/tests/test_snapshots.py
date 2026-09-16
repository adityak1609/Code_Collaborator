"""Milestone 3 snapshot creation and CRDT-safe restore tests."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pycrdt
import pytest
from pydantic import ValidationError

from app.collaboration import websocket as websocket_module
from app.collaboration.document import DocumentManager
from app.snapshots import router as snapshots_router
from app.snapshots.schemas import SnapshotCreate


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def set(self, key: str, value: bytes, *, ex: int | None = None) -> bool:
        self.values[key] = value
        return True

    async def aclose(self) -> None:
        return None


def yjs_state(text: str) -> bytes:
    doc = pycrdt.Doc()
    doc["monaco"] = pycrdt.Text()
    doc["monaco"].insert(0, text)
    return doc.get_update()


def test_snapshot_label_rejects_whitespace_only() -> None:
    with pytest.raises(ValidationError):
        SnapshotCreate(label="   ")


@pytest.mark.asyncio
async def test_restore_text_is_an_incremental_update_for_connected_peers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = str(uuid.uuid4())
    manager = DocumentManager(FakeRedis())
    monkeypatch.setattr(
        manager,
        "_load_db_state",
        AsyncMock(return_value=yjs_state("current draft")),
    )
    await manager.load_from_storage(session_id)

    peer = pycrdt.Doc()
    peer["monaco"] = pycrdt.Text()
    peer.apply_update(manager.encode_state_as_update(session_id))

    update = manager.restore_text_from_state(session_id, yjs_state("checkpoint"))
    peer.apply_update(update)

    assert manager.read_text(session_id) == "checkpoint"
    assert str(peer["monaco"]) == "checkpoint"
    assert update
    assert manager.get_document_status(session_id).dirty is True


class FakeSnapshotManager:
    def __init__(self, state: bytes) -> None:
        self.state = state
        self.load_from_storage = AsyncMock()
        self.checkpoint_to_redis = AsyncMock()
        self.checkpoint_and_remove = AsyncMock(return_value=True)
        self.restore_text_from_state = Mock(return_value=b"restore-update")

    def encode_state_as_update(self, session_id: str) -> bytes:
        return self.state


def snapshot_record(session_id: uuid.UUID, user_id: uuid.UUID):
    return SimpleNamespace(
        id=uuid.uuid4(),
        session_id=session_id,
        created_by=user_id,
        label="Before refactor",
        document_state=yjs_state("before"),
        size_bytes=len(yjs_state("before")),
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_create_snapshot_uses_live_authoritative_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4())
    manager = FakeSnapshotManager(yjs_state("live draft"))
    record = snapshot_record(session_id, user.id)
    create = AsyncMock(return_value=record)
    monkeypatch.setattr(snapshots_router, "doc_manager", manager)
    monkeypatch.setattr(snapshots_router, "create_snapshot", create)
    monkeypatch.setattr(snapshots_router, "_require_current_editor", AsyncMock())
    monkeypatch.setattr(websocket_module, "_rooms", {str(session_id): [(object(),)]})
    monkeypatch.setattr(websocket_module, "_room_locks", {})

    result = await snapshots_router.create_snapshot_endpoint(
        session_id=session_id,
        body=SnapshotCreate(label=" Before refactor "),
        current_user=user,
        db=SimpleNamespace(),
    )

    assert result is record
    manager.load_from_storage.assert_awaited_once_with(str(session_id))
    assert create.await_args.kwargs["document_state"] == manager.state
    manager.checkpoint_and_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_snapshot_checkpoints_and_broadcasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4())
    manager = FakeSnapshotManager(yjs_state("current"))
    record = snapshot_record(session_id, user.id)
    broadcast = AsyncMock()
    monkeypatch.setattr(snapshots_router, "doc_manager", manager)
    monkeypatch.setattr(
        snapshots_router, "get_snapshot", AsyncMock(return_value=record)
    )
    monkeypatch.setattr(snapshots_router, "_require_current_editor", AsyncMock())
    monkeypatch.setattr(snapshots_router, "broadcast_document_update", broadcast)
    monkeypatch.setattr(websocket_module, "_rooms", {str(session_id): [(object(),)]})
    monkeypatch.setattr(websocket_module, "_room_locks", {})

    result = await snapshots_router.restore_snapshot_endpoint(
        session_id=session_id,
        snapshot_id=record.id,
        current_user=user,
        db=SimpleNamespace(),
    )

    manager.restore_text_from_state.assert_called_once_with(
        str(session_id), record.document_state
    )
    manager.checkpoint_to_redis.assert_awaited_once_with(str(session_id))
    broadcast.assert_awaited_once_with(str(session_id), b"restore-update")
    assert result.update_size_bytes == len(b"restore-update")
