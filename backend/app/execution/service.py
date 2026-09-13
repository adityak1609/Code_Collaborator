"""Execution persistence, queueing, history, and cancellation rules."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution.queue import ExecutionQueue
from app.models import Execution, ExecutionStatus, LanguageEnum


class ExecutionServiceError(Exception):
    """Base class for expected execution-domain failures."""


class ExecutionNotFoundError(ExecutionServiceError):
    """The requested execution does not exist in this session."""


class ExecutionConflictError(ExecutionServiceError):
    """The requested operation conflicts with the execution state."""


class ExecutionQueueError(ExecutionServiceError):
    """The job could not be handed to Redis."""


async def create_queued_execution(
    db: AsyncSession,
    queue: ExecutionQueue,
    *,
    session_id: uuid.UUID,
    triggered_by: uuid.UUID,
    code: str,
    language: LanguageEnum,
) -> Execution:
    """Persist a queued execution, then hand its identifier to Redis."""
    execution = Execution(
        session_id=session_id,
        triggered_by=triggered_by,
        status=ExecutionStatus.QUEUED,
        code=code,
        language=language,
    )
    db.add(execution)
    await db.commit()
    await db.refresh(execution)

    try:
        await queue.enqueue(execution.id)
    except Exception as exc:
        # Do not expose a permanently QUEUED record when Redis never accepted
        # the job. The worker receives only committed execution identifiers.
        await db.delete(execution)
        await db.commit()
        raise ExecutionQueueError("Execution queue is unavailable") from exc
    return execution


async def get_execution(
    db: AsyncSession,
    session_id: uuid.UUID,
    execution_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Execution:
    statement = select(Execution).where(
        Execution.id == execution_id,
        Execution.session_id == session_id,
    )
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    execution = result.scalar_one_or_none()
    if execution is None:
        raise ExecutionNotFoundError("Execution not found")
    return execution


async def list_executions(
    db: AsyncSession,
    session_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[Execution], int]:
    total_result = await db.execute(
        select(func.count())
        .select_from(Execution)
        .where(Execution.session_id == session_id)
    )
    result = await db.execute(
        select(Execution)
        .where(Execution.session_id == session_id)
        .order_by(Execution.created_at.desc(), Execution.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), int(total_result.scalar_one())


async def cancel_execution(
    db: AsyncSession,
    queue: ExecutionQueue,
    *,
    session_id: uuid.UUID,
    execution_id: uuid.UUID,
) -> Execution:
    execution = await get_execution(
        db,
        session_id,
        execution_id,
        for_update=True,
    )
    if execution.status not in {ExecutionStatus.QUEUED, ExecutionStatus.RUNNING}:
        raise ExecutionConflictError(
            f"Execution in state {execution.status.value} cannot be cancelled"
        )

    if execution.status is ExecutionStatus.RUNNING:
        try:
            await queue.request_cancellation(execution.id)
        except Exception as exc:
            raise ExecutionQueueError("Cancellation service is unavailable") from exc

    execution.transition_to(ExecutionStatus.CANCELLED)
    execution.finished_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(execution)
    return execution
