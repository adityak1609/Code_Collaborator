"""Named collaborative-document snapshots."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import RoleChecker
from app.collaboration.document import doc_manager
from app.collaboration.websocket import (
    broadcast_document_update,
    document_room_lifecycle,
)
from app.config import settings
from app.database import get_db
from app.models import RoleEnum, User
from app.sessions.router import _require_current_editor
from app.snapshots.schemas import (
    SnapshotCreate,
    SnapshotHistoryResponse,
    SnapshotResponse,
    SnapshotRestoreResponse,
)
from app.snapshots.service import (
    SnapshotNotFoundError,
    create_snapshot,
    get_snapshot,
    list_snapshots,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/sessions/{session_id}/snapshots",
    tags=["snapshots"],
)


@router.post("", response_model=SnapshotResponse, status_code=status.HTTP_201_CREATED)
async def create_snapshot_endpoint(
    session_id: uuid.UUID,
    body: SnapshotCreate,
    current_user: User = Depends(RoleChecker(min_role=RoleEnum.editor)),
    db: AsyncSession = Depends(get_db),
):
    """Capture the authoritative live Yjs state under a user-provided label."""
    canonical_session_id = str(session_id)
    async with document_room_lifecycle(canonical_session_id) as has_connections:
        await _require_current_editor(db, session_id, current_user.id)
        await doc_manager.load_from_storage(canonical_session_id)
        document_state = doc_manager.encode_state_as_update(canonical_session_id)
        if len(document_state) > settings.crdt_max_update_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Document state exceeds the configured size limit",
            )
        snapshot = await create_snapshot(
            db,
            session_id=session_id,
            created_by=current_user.id,
            label=body.label,
            document_state=document_state,
        )
        if not has_connections:
            try:
                await doc_manager.checkpoint_and_remove(canonical_session_id)
            except Exception:
                logger.exception(
                    "Failed to evict document after snapshot: session=%s",
                    canonical_session_id,
                )
    return snapshot


@router.get("", response_model=SnapshotHistoryResponse)
async def list_snapshots_endpoint(
    session_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _current_user: User = Depends(RoleChecker(min_role=RoleEnum.viewer)),
    db: AsyncSession = Depends(get_db),
):
    """List snapshot metadata; document bytes are never returned in history."""
    items, total = await list_snapshots(
        db,
        session_id,
        limit=limit,
        offset=offset,
    )
    return SnapshotHistoryResponse(
        items=[SnapshotResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/{snapshot_id}/restore", response_model=SnapshotRestoreResponse)
async def restore_snapshot_endpoint(
    session_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    current_user: User = Depends(RoleChecker(min_role=RoleEnum.editor)),
    db: AsyncSession = Depends(get_db),
):
    """Restore snapshot text as a normal Yjs edit visible to live peers."""
    canonical_session_id = str(session_id)
    async with document_room_lifecycle(canonical_session_id) as has_connections:
        # Close the authorization race between the dependency and mutation.
        await _require_current_editor(db, session_id, current_user.id)
        try:
            snapshot = await get_snapshot(db, session_id, snapshot_id)
        except SnapshotNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if snapshot.size_bytes > settings.crdt_max_update_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Snapshot state exceeds the configured size limit",
            )

        await doc_manager.load_from_storage(canonical_session_id)
        try:
            update = doc_manager.restore_text_from_state(
                canonical_session_id,
                snapshot.document_state,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Snapshot contains invalid Yjs document state",
            ) from exc

        try:
            if has_connections:
                await doc_manager.checkpoint_to_redis(canonical_session_id)
            else:
                await doc_manager.checkpoint_and_remove(canonical_session_id)
        except Exception:
            logger.exception(
                "Failed to checkpoint restored snapshot: session=%s",
                canonical_session_id,
            )

    if update:
        await broadcast_document_update(canonical_session_id, update)
    return SnapshotRestoreResponse(
        snapshot=SnapshotResponse.model_validate(snapshot),
        update_size_bytes=len(update),
    )
