"""Focused tests for the execution queue and service state boundaries."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.execution.queue import EXECUTION_QUEUE_KEY, ExecutionQueue
from app.execution.service import (
    ExecutionConflictError,
    ExecutionQueueError,
    cancel_execution,
    create_queued_execution,
)
from app.models import Execution, ExecutionStatus, LanguageEnum


class FakeRedis:
    def __init__(self) -> None:
        self.rpush = AsyncMock()
        self.set = AsyncMock()
        self.aclose = AsyncMock()


@pytest.mark.asyncio
async def test_execution_queue_uses_stable_keys():
    redis = FakeRedis()
    queue = ExecutionQueue(redis)
    execution_id = uuid.uuid4()

    await queue.enqueue(execution_id)
    await queue.request_cancellation(execution_id)
    await queue.close()

    redis.rpush.assert_awaited_once_with(EXECUTION_QUEUE_KEY, str(execution_id))
    assert redis.set.await_args.args == (
        queue.cancellation_key(execution_id),
        "1",
    )
    assert redis.set.await_args.kwargs["ex"] > 0
    redis.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_execution_persists_before_enqueue():
    events: list[str] = []
    execution_id = uuid.uuid4()
    db = SimpleNamespace(
        add=lambda execution: events.append("add"),
        commit=AsyncMock(side_effect=lambda: events.append("commit")),
        refresh=AsyncMock(
            side_effect=lambda execution: (
                setattr(execution, "id", execution_id),
                events.append("refresh"),
            )
        ),
        delete=AsyncMock(),
    )
    queue = SimpleNamespace(
        enqueue=AsyncMock(side_effect=lambda _execution_id: events.append("enqueue"))
    )

    result = await create_queued_execution(
        db,
        queue,
        session_id=uuid.uuid4(),
        triggered_by=uuid.uuid4(),
        code="print('ok')",
        language=LanguageEnum.python,
    )

    assert result.id == execution_id
    assert result.status is ExecutionStatus.QUEUED
    assert events == ["add", "commit", "refresh", "enqueue"]


@pytest.mark.asyncio
async def test_enqueue_failure_removes_unaccepted_execution():
    db = SimpleNamespace(
        add=MagicMock(),
        commit=AsyncMock(),
        refresh=AsyncMock(),
        delete=AsyncMock(),
    )
    queue = SimpleNamespace(enqueue=AsyncMock(side_effect=ConnectionError))

    with pytest.raises(ExecutionQueueError, match="unavailable"):
        await create_queued_execution(
            db,
            queue,
            session_id=uuid.uuid4(),
            triggered_by=uuid.uuid4(),
            code="",
            language=LanguageEnum.javascript,
        )

    db.delete.assert_awaited_once()
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_cancel_queued_execution_is_terminal_without_redis_signal(monkeypatch):
    execution = Execution(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        triggered_by=uuid.uuid4(),
        status=ExecutionStatus.QUEUED,
        code="",
        language=LanguageEnum.python,
    )
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    queue = SimpleNamespace(request_cancellation=AsyncMock())
    monkeypatch.setattr(
        "app.execution.service.get_execution",
        AsyncMock(return_value=execution),
    )

    result = await cancel_execution(
        db,
        queue,
        session_id=execution.session_id,
        execution_id=execution.id,
    )

    assert result.status is ExecutionStatus.CANCELLED
    assert result.finished_at is not None
    queue.request_cancellation.assert_not_awaited()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_rejects_non_cancellable_state(monkeypatch):
    execution = Execution(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        triggered_by=uuid.uuid4(),
        status=ExecutionStatus.COMPLETED,
        code="",
        language=LanguageEnum.python,
    )
    monkeypatch.setattr(
        "app.execution.service.get_execution",
        AsyncMock(return_value=execution),
    )

    with pytest.raises(ExecutionConflictError, match="cannot be cancelled"):
        await cancel_execution(
            SimpleNamespace(),
            SimpleNamespace(),
            session_id=execution.session_id,
            execution_id=execution.id,
        )
