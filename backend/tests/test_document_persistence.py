"""Tests for Redis recovery checkpoints and explicit document saves."""

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pycrdt
import pytest
from fastapi import HTTPException, status

from app.collaboration import document as document_module
from app.collaboration import websocket as websocket_module
from app.collaboration.document import DocumentManager, DocumentSaveResult
from app.sessions import router as sessions_router


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.expirations: dict[str, int | None] = {}
        self.closed = False
        self.get_error: Exception | None = None

    async def get(self, key: str) -> bytes | None:
        if self.get_error is not None:
            raise self.get_error
        return self.values.get(key)

    async def set(
        self,
        key: str,
        value: bytes,
        *,
        ex: int | None = None,
    ) -> bool:
        self.values[key] = value
        self.expirations[key] = ex
        return True

    async def aclose(self) -> None:
        self.closed = True


def yjs_state(text: str) -> bytes:
    doc = pycrdt.Doc()
    doc["monaco"] = pycrdt.Text()
    doc["monaco"].insert(0, text)
    return doc.get_update()


def extend_yjs_state(state: bytes, suffix: str) -> bytes:
    doc = pycrdt.Doc()
    doc["monaco"] = pycrdt.Text()
    doc.apply_update(state)
    text: pycrdt.Text = doc["monaco"]
    text.insert(len(str(text)), suffix)
    return doc.get_update()


def delete_yjs_text(state: bytes) -> bytes:
    doc = pycrdt.Doc()
    doc["monaco"] = pycrdt.Text()
    doc.apply_update(state)
    text: pycrdt.Text = doc["monaco"]
    text.clear()
    return doc.get_update()


def decode_yjs_text(state: bytes) -> str:
    doc = pycrdt.Doc()
    doc["monaco"] = pycrdt.Text()
    doc.apply_update(state)
    return str(doc["monaco"])


class BlockingRedis(FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.first_set_started = asyncio.Event()
        self.allow_first_set = asyncio.Event()
        self.set_calls = 0

    async def set(
        self,
        key: str,
        value: bytes,
        *,
        ex: int | None = None,
    ) -> bool:
        self.set_calls += 1
        if self.set_calls == 1:
            self.first_set_started.set()
            await self.allow_first_set.wait()
        return await super().set(key, value, ex=ex)


@pytest.mark.asyncio
async def test_load_merges_explicit_save_and_newer_redis_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = str(uuid.uuid4())
    saved_state = yjs_state("saved")

    recovered = pycrdt.Doc()
    recovered["monaco"] = pycrdt.Text()
    recovered.apply_update(saved_state)
    recovered["monaco"].insert(5, " + recovered")

    redis = FakeRedis()
    redis.values[DocumentManager.checkpoint_key(session_id)] = recovered.get_update()
    manager = DocumentManager(redis)
    monkeypatch.setattr(
        manager,
        "_load_db_state",
        AsyncMock(return_value=saved_state),
    )

    await manager.load_from_storage(session_id)

    assert manager.read_text(session_id) == "saved + recovered"


@pytest.mark.asyncio
async def test_redis_outage_falls_back_to_explicit_database_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = str(uuid.uuid4())
    redis = FakeRedis()
    redis.get_error = ConnectionError("redis unavailable")
    manager = DocumentManager(redis)
    monkeypatch.setattr(
        manager,
        "_load_db_state",
        AsyncMock(return_value=yjs_state("durable")),
    )

    await manager.load_from_storage(session_id)

    assert manager.read_text(session_id) == "durable"


@pytest.mark.asyncio
async def test_first_load_is_serialized_and_hidden_until_recovery_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = str(uuid.uuid4())
    saved_state = yjs_state("saved")
    recovered_state = extend_yjs_state(saved_state, " + recovered")
    saved_at = datetime.now(UTC)
    redis = FakeRedis()
    redis.values[DocumentManager.checkpoint_key(session_id)] = recovered_state
    get_started = asyncio.Event()
    allow_get = asyncio.Event()

    async def blocked_get(key: str) -> bytes | None:
        get_started.set()
        await allow_get.wait()
        return redis.values.get(key)

    monkeypatch.setattr(redis, "get", blocked_get)
    manager = DocumentManager(redis)
    load_db_state = AsyncMock(
        return_value=SimpleNamespace(state=saved_state, saved_at=saved_at)
    )
    monkeypatch.setattr(manager, "_load_db_state", load_db_state)

    first = asyncio.create_task(manager.load_from_storage(session_id))
    await asyncio.wait_for(get_started.wait(), timeout=1)

    assert manager.active_sessions == []
    assert manager.read_text(session_id) == ""
    with pytest.raises(RuntimeError, match="still loading"):
        manager.get_or_create(session_id)

    second_attempted = asyncio.Event()

    async def load_again() -> pycrdt.Doc:
        second_attempted.set()
        return await manager.load_from_storage(session_id)

    second = asyncio.create_task(load_again())
    await asyncio.wait_for(second_attempted.wait(), timeout=1)
    assert second.done() is False

    allow_get.set()
    first_doc, second_doc = await asyncio.gather(first, second)

    assert first_doc is second_doc
    assert manager.read_text(session_id) == "saved + recovered"
    assert manager.active_sessions == [session_id]
    assert load_db_state.await_count == 1
    status_result = manager.get_document_status(session_id)
    assert status_result is not None
    assert status_result.saved_at == saved_at
    assert status_result.dirty is True
    assert status_result.generation == 0
    assert status_result.checkpointed_generation == 0

    saved_doc = pycrdt.Doc()
    saved_doc["monaco"] = pycrdt.Text()
    saved_doc.apply_update(saved_state)
    assert status_result.saved_state_vector == saved_doc.get_state()
    assert status_result.saved_state_hash == hashlib.sha256(saved_state).hexdigest()


@pytest.mark.asyncio
async def test_checkpoint_round_trip_and_close() -> None:
    session_id = str(uuid.uuid4())
    redis = FakeRedis()
    manager = DocumentManager(redis)
    manager.apply_update(session_id, yjs_state("checkpoint me"))

    size = await manager.checkpoint_to_redis(session_id)
    await manager.close()

    stored = redis.values[DocumentManager.checkpoint_key(session_id)]
    restored = pycrdt.Doc()
    restored["monaco"] = pycrdt.Text()
    restored.apply_update(stored)
    assert str(restored["monaco"]) == "checkpoint me"
    assert size == len(stored)
    assert redis.expirations[DocumentManager.checkpoint_key(session_id)] == 604800
    assert redis.closed


@pytest.mark.asyncio
async def test_concurrent_checkpoints_are_ordered_and_latest_generation_wins() -> None:
    session_id = str(uuid.uuid4())
    initial_state = yjs_state("first")
    newer_state = extend_yjs_state(initial_state, " + second")
    redis = BlockingRedis()
    manager = DocumentManager(redis)
    manager.apply_update(session_id, initial_state)

    first = asyncio.create_task(manager.checkpoint_to_redis(session_id))
    await asyncio.wait_for(redis.first_set_started.wait(), timeout=1)

    manager.apply_update(session_id, newer_state)
    second_attempted = asyncio.Event()

    async def checkpoint_again() -> int:
        second_attempted.set()
        return await manager.checkpoint_to_redis(session_id)

    second = asyncio.create_task(checkpoint_again())
    await asyncio.wait_for(second_attempted.wait(), timeout=1)
    assert redis.set_calls == 1

    redis.allow_first_set.set()
    await asyncio.gather(first, second)

    stored = redis.values[DocumentManager.checkpoint_key(session_id)]
    assert decode_yjs_text(stored) == "first + second"
    status_result = manager.get_document_status(session_id)
    assert status_result is not None
    assert status_result.generation == 2
    assert status_result.checkpointed_generation == 2


@pytest.mark.asyncio
async def test_checkpoint_and_remove_retains_edit_during_redis_io() -> None:
    session_id = str(uuid.uuid4())
    initial_state = yjs_state("first")
    newer_state = extend_yjs_state(initial_state, " + concurrent")
    redis = BlockingRedis()
    manager = DocumentManager(redis)
    manager.apply_update(session_id, initial_state)

    removal = asyncio.create_task(manager.checkpoint_and_remove(session_id))
    await asyncio.wait_for(redis.first_set_started.wait(), timeout=1)
    manager.apply_update(session_id, newer_state)
    redis.allow_first_set.set()

    assert await removal is False
    assert session_id in manager.active_sessions
    status_result = manager.get_document_status(session_id)
    assert status_result is not None
    assert status_result.checkpointed_generation == 1
    assert status_result.generation == 2

    assert await manager.checkpoint_and_remove(session_id) is True
    assert session_id not in manager.active_sessions
    assert manager.get_document_status(session_id) is None
    assert session_id not in manager._guards
    stored = redis.values[DocumentManager.checkpoint_key(session_id)]
    assert decode_yjs_text(stored) == "first + concurrent"


@pytest.mark.asyncio
async def test_explicit_save_persists_full_document_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = str(uuid.uuid4())
    manager = DocumentManager(FakeRedis())
    manager.apply_update(session_id, yjs_state("save me"))
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    class SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(
        document_module,
        "async_session_factory",
        lambda: SessionContext(),
    )

    result = await manager.persist_to_db(session_id)

    assert result.size_bytes == len(manager.encode_state_as_update(session_id))
    assert result.state_vector == manager.get_state_vector(session_id)
    assert (
        result.state_hash
        == hashlib.sha256(manager.encode_state_as_update(session_id)).hexdigest()
    )
    assert result.dirty is False
    db.execute.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_database_saves_cannot_complete_out_of_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = str(uuid.uuid4())
    initial_state = yjs_state("first")
    newer_state = extend_yjs_state(initial_state, " + second")
    manager = DocumentManager(FakeRedis())
    manager.apply_update(session_id, initial_state)

    first_execute_started = asyncio.Event()
    allow_first_execute = asyncio.Event()
    writes: list[bytes] = []
    commits: list[bytes] = []

    class ControlledDatabase:
        def __init__(self) -> None:
            self.state: bytes | None = None

        async def execute(self, statement):
            self.state = statement.compile().params["yjs_state"]
            call_index = len(writes)
            writes.append(self.state)
            if call_index == 0:
                first_execute_started.set()
                await allow_first_execute.wait()

        async def commit(self) -> None:
            assert self.state is not None
            commits.append(self.state)

    class SessionContext:
        async def __aenter__(self):
            return ControlledDatabase()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(
        document_module,
        "async_session_factory",
        SessionContext,
    )

    first = asyncio.create_task(manager.persist_to_db(session_id))
    await asyncio.wait_for(first_execute_started.wait(), timeout=1)
    manager.apply_update(session_id, newer_state)

    second_attempted = asyncio.Event()

    async def save_again() -> DocumentSaveResult:
        second_attempted.set()
        return await manager.persist_to_db(session_id)

    second = asyncio.create_task(save_again())
    await asyncio.wait_for(second_attempted.wait(), timeout=1)
    assert len(writes) == 1

    allow_first_execute.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert commits == writes
    assert len(commits) == 2
    assert decode_yjs_text(commits[0]) == "first"
    assert decode_yjs_text(commits[1]) == "first + second"
    assert first_result.dirty is True
    assert second_result.dirty is False
    status_result = manager.get_document_status(session_id)
    assert status_result is not None
    assert status_result.saved_state_vector == second_result.state_vector
    assert status_result.dirty is False


@pytest.mark.asyncio
async def test_delete_only_update_is_dirty_and_advances_checkpoint_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = str(uuid.uuid4())
    manager = DocumentManager(FakeRedis())
    inserted_state = yjs_state("delete me")
    manager.apply_update(session_id, inserted_state)
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    class SessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(document_module, "async_session_factory", SessionContext)
    saved = await manager.persist_to_db(session_id)
    generation_before_delete = manager.get_document_status(session_id).generation

    deleted_state = delete_yjs_text(inserted_state)
    manager.apply_update(session_id, deleted_state)
    status_result = manager.get_document_status(session_id)

    assert manager.get_state_vector(session_id) == saved.state_vector
    assert status_result is not None
    assert status_result.generation == generation_before_delete + 1
    assert status_result.dirty is True
    assert status_result.saved_state_hash == saved.state_hash
    assert hashlib.sha256(manager.encode_state_as_update(session_id)).hexdigest() != (
        saved.state_hash
    )


class FakeSaveManager:
    def __init__(self, session_id: str) -> None:
        self.active_sessions = [session_id]
        self.applied: list[tuple[str, bytes]] = []
        self.load_from_storage = AsyncMock()
        self.checkpoint_to_redis = AsyncMock()
        self.checkpoint_and_remove = AsyncMock(return_value=True)
        self.persist_to_db = AsyncMock(
            return_value=DocumentSaveResult(
                size_bytes=42,
                saved_at=document_module.datetime.now(document_module.UTC),
                state_vector=b"saved-state-vector",
                state_hash="a" * 64,
                dirty=False,
            )
        )

    def apply_update(self, session_id: str, update: bytes) -> None:
        self.applied.append((session_id, update))


@pytest.mark.asyncio
async def test_save_endpoint_merges_persists_checkpoints_and_broadcasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    manager = FakeSaveManager(str(session_id))
    broadcast = AsyncMock()
    broadcast_saved = AsyncMock()
    db = SimpleNamespace()
    monkeypatch.setattr(sessions_router, "doc_manager", manager)
    recheck = AsyncMock()
    monkeypatch.setattr(sessions_router, "_require_current_editor", recheck)
    monkeypatch.setattr(
        websocket_module,
        "_rooms",
        {str(session_id): [(object(), "user", "ada", None)]},
    )
    monkeypatch.setattr(websocket_module, "_room_locks", {})
    monkeypatch.setattr(websocket_module, "broadcast_document_update", broadcast)
    monkeypatch.setattr(
        websocket_module,
        "broadcast_document_saved",
        broadcast_saved,
    )

    response = await sessions_router.save_session_document(
        session_id=session_id,
        document_state=b"client-state",
        current_user=SimpleNamespace(id=uuid.uuid4(), username="ada"),
        db=db,
    )

    assert response.session_id == session_id
    assert response.size_bytes == 42
    assert response.state_vector == "c2F2ZWQtc3RhdGUtdmVjdG9y"
    assert response.state_hash == "a" * 64
    assert response.dirty is False
    assert manager.applied == [(str(session_id), b"client-state")]
    recheck.assert_awaited_once()
    assert recheck.await_args.args[0] is db
    manager.load_from_storage.assert_awaited_once_with(str(session_id))
    manager.persist_to_db.assert_awaited_once_with(str(session_id))
    manager.checkpoint_to_redis.assert_awaited_once_with(str(session_id))
    manager.checkpoint_and_remove.assert_not_awaited()
    broadcast.assert_awaited_once_with(str(session_id), b"client-state")
    broadcast_saved.assert_awaited_once_with(
        str(session_id),
        saved_at=manager.persist_to_db.return_value.saved_at,
        state_vector=b"saved-state-vector",
        state_hash="a" * 64,
        dirty=False,
        saved_by="ada",
    )


@pytest.mark.asyncio
async def test_save_endpoint_evicts_document_loaded_without_a_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid.uuid4()
    manager = FakeSaveManager(str(session_id))
    db = SimpleNamespace()
    monkeypatch.setattr(sessions_router, "doc_manager", manager)
    monkeypatch.setattr(
        sessions_router,
        "_require_current_editor",
        AsyncMock(),
    )
    monkeypatch.setattr(websocket_module, "_rooms", {})
    monkeypatch.setattr(websocket_module, "_room_locks", {})
    monkeypatch.setattr(
        websocket_module,
        "broadcast_document_update",
        AsyncMock(),
    )
    monkeypatch.setattr(
        websocket_module,
        "broadcast_document_saved",
        AsyncMock(),
    )

    await sessions_router.save_session_document(
        session_id=session_id,
        document_state=b"client-state",
        current_user=SimpleNamespace(id=uuid.uuid4(), username="ada"),
        db=db,
    )

    manager.checkpoint_and_remove.assert_awaited_once_with(str(session_id))
    manager.checkpoint_to_redis.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_endpoint_rejects_empty_document_state() -> None:
    with pytest.raises(HTTPException) as caught:
        await sessions_router.save_session_document(
            session_id=uuid.uuid4(),
            document_state=b"",
            current_user=SimpleNamespace(id=uuid.uuid4()),
        )

    assert caught.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
