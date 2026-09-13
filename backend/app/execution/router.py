"""RBAC-protected execution submission, history, detail, and cancellation."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import RoleChecker
from app.collaboration.document import doc_manager
from app.collaboration.websocket import document_room_lifecycle
from app.database import get_db
from app.execution.queue import execution_queue
from app.execution.schemas import ExecutionHistoryResponse, ExecutionResponse
from app.execution.service import (
    ExecutionConflictError,
    ExecutionNotFoundError,
    ExecutionQueueError,
    cancel_execution,
    create_queued_execution,
    get_execution,
    list_executions,
)
from app.models import RoleEnum, Session, User
from app.sessions.router import _require_current_editor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions/{session_id}", tags=["executions"])


def _raise_execution_error(error: Exception) -> None:
    if isinstance(error, ExecutionNotFoundError):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, ExecutionConflictError):
        raise HTTPException(status_code=409, detail=str(error)) from error
    if isinstance(error, ExecutionQueueError):
        raise HTTPException(status_code=503, detail=str(error)) from error
    raise error


@router.post(
    "/run",
    response_model=ExecutionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_document(
    session_id: uuid.UUID,
    current_user: User = Depends(RoleChecker(min_role=RoleEnum.editor)),
    db: AsyncSession = Depends(get_db),
):
    """Capture the current CRDT text and enqueue it for isolated execution."""
    canonical_session_id = str(session_id)
    async with document_room_lifecycle(canonical_session_id) as has_connections:
        await _require_current_editor(db, session_id, current_user.id)
        session = await db.get(Session, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")

        await doc_manager.load_from_storage(canonical_session_id)
        code = doc_manager.read_text(canonical_session_id)
        if not has_connections:
            try:
                await doc_manager.checkpoint_and_remove(canonical_session_id)
            except Exception:
                logger.exception(
                    "Failed to evict document loaded for execution %s",
                    canonical_session_id,
                )

        language = session.language

    # Redis latency must not hold the collaboration mutation gate after the
    # authorized document snapshot has been captured.
    try:
        return await create_queued_execution(
            db,
            execution_queue,
            session_id=session_id,
            triggered_by=current_user.id,
            code=code,
            language=language,
        )
    except ExecutionQueueError as error:
        _raise_execution_error(error)


@router.get("/executions", response_model=ExecutionHistoryResponse)
async def execution_history(
    session_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _current_user: User = Depends(RoleChecker(min_role=RoleEnum.viewer)),
    db: AsyncSession = Depends(get_db),
):
    items, total = await list_executions(
        db,
        session_id,
        limit=limit,
        offset=offset,
    )
    return ExecutionHistoryResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/executions/{execution_id}", response_model=ExecutionResponse)
async def execution_detail(
    session_id: uuid.UUID,
    execution_id: uuid.UUID,
    _current_user: User = Depends(RoleChecker(min_role=RoleEnum.viewer)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await get_execution(db, session_id, execution_id)
    except ExecutionNotFoundError as error:
        _raise_execution_error(error)


@router.post(
    "/executions/{execution_id}/cancel",
    response_model=ExecutionResponse,
)
async def cancel_execution_endpoint(
    session_id: uuid.UUID,
    execution_id: uuid.UUID,
    current_user: User = Depends(RoleChecker(min_role=RoleEnum.editor)),
    db: AsyncSession = Depends(get_db),
):
    async with document_room_lifecycle(str(session_id)):
        await _require_current_editor(db, session_id, current_user.id)
        try:
            return await cancel_execution(
                db,
                execution_queue,
                session_id=session_id,
                execution_id=execution_id,
            )
        except (
            ExecutionNotFoundError,
            ExecutionConflictError,
            ExecutionQueueError,
        ) as error:
            _raise_execution_error(error)
