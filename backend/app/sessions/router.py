"""Session management endpoints — CRUD + member management."""

import base64
import logging
import uuid
from typing import NoReturn

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import RoleChecker, get_current_user
from app.collaboration.document import doc_manager
from app.config import settings
from app.database import get_db
from app.models import ROLE_RANK, RoleEnum, Session, SessionMember, User
from app.sessions.schemas import (
    AddMemberRequest,
    DocumentSaveResponse,
    MemberResponse,
    SessionCreate,
    SessionDetailResponse,
    SessionResponse,
    UpdateMemberRoleRequest,
)
from app.sessions.service import (
    SessionConflictError,
    SessionInactiveError,
    SessionResourceNotFoundError,
    SessionServiceError,
    add_member,
    close_session,
    create_session,
    get_session_detail,
    list_user_sessions,
    remove_member,
    update_member_role,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _raise_http_error(error: SessionServiceError) -> NoReturn:
    """Translate expected service failures into stable API responses."""
    if isinstance(error, SessionResourceNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, SessionInactiveError):
        status_code = status.HTTP_410_GONE
    elif isinstance(error, SessionConflictError):
        status_code = status.HTTP_409_CONFLICT
    else:  # Defensive fallback for future expected service failures.
        status_code = status.HTTP_400_BAD_REQUEST

    raise HTTPException(status_code=status_code, detail=str(error)) from error


async def _require_current_editor(
    db: AsyncSession,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Revalidate save authority inside the per-session mutation gate."""
    result = await db.execute(
        select(Session.is_active, SessionMember.role)
        .outerjoin(
            SessionMember,
            and_(
                SessionMember.session_id == Session.id,
                SessionMember.user_id == user_id,
            ),
        )
        .where(Session.id == session_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not row.is_active:
        raise HTTPException(status_code=410, detail="Session is closed")
    if row.role is None or ROLE_RANK[row.role] < ROLE_RANK[RoleEnum.editor]:
        raise HTTPException(status_code=403, detail="Insufficient permissions")


async def _resolve_member_user_id(
    db: AsyncSession,
    body: AddMemberRequest,
) -> uuid.UUID:
    """Resolve a username invitation while preserving the ID-based API."""
    if body.user_id is not None:
        return body.user_id

    result = await db.execute(select(User.id).where(User.username == body.username))
    user_id = result.scalar_one_or_none()
    if user_id is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user_id


@router.post(
    "",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_session_endpoint(
    body: SessionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new session — the caller becomes the owner."""
    session = await create_session(
        db=db,
        name=body.name,
        language=body.language,
        owner=current_user,
    )
    return session


@router.get("", response_model=list[SessionResponse])
async def list_sessions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all sessions the authenticated user belongs to."""
    return await list_user_sessions(db, current_user.id)


@router.get("/{session_id}", response_model=SessionDetailResponse)
async def get_session(
    session_id: uuid.UUID,
    current_user: User = Depends(RoleChecker(min_role=RoleEnum.viewer)),
    db: AsyncSession = Depends(get_db),
):
    """Get session details including member list."""
    session = await get_session_detail(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Build member response with usernames
    members = [
        MemberResponse(
            user_id=m.user_id,
            username=m.user.username,
            role=m.role,
            joined_at=m.joined_at,
        )
        for m in session.members
    ]

    return SessionDetailResponse(
        id=session.id,
        name=session.name,
        language=session.language,
        owner_id=session.owner_id,
        is_active=session.is_active,
        created_at=session.created_at,
        updated_at=session.updated_at,
        members=members,
    )


@router.post("/{session_id}/save", response_model=DocumentSaveResponse)
async def save_session_document(
    session_id: uuid.UUID,
    document_state: bytes = Body(media_type="application/octet-stream"),
    current_user: User = Depends(RoleChecker(min_role=RoleEnum.editor)),
    db: AsyncSession = Depends(get_db),
):
    """Merge the caller's Yjs state and explicitly persist it to PostgreSQL."""
    if not document_state:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Document state cannot be empty",
        )
    if len(document_state) > settings.crdt_max_update_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Document state exceeds the configured size limit",
        )

    canonical_session_id = str(session_id)
    from app.collaboration.websocket import document_room_lifecycle

    async with document_room_lifecycle(canonical_session_id) as has_connections:
        await _require_current_editor(db, session_id, current_user.id)
        await doc_manager.load_from_storage(canonical_session_id)
        try:
            doc_manager.apply_update(canonical_session_id, document_state)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invalid Yjs document state",
            ) from exc

        result = await doc_manager.persist_to_db(canonical_session_id)

        # Keep recovery aligned with the explicit save, but do not turn a
        # Redis outage into a failed PostgreSQL save. A document loaded only
        # for this request is evicted under the room lifecycle lock.
        try:
            if has_connections:
                await doc_manager.checkpoint_to_redis(canonical_session_id)
            else:
                await doc_manager.checkpoint_and_remove(canonical_session_id)
        except Exception:
            logger.exception(
                "Failed to checkpoint explicitly saved session %s",
                canonical_session_id,
            )

    # The upload closes the WebSocket/HTTP ordering race and this broadcast
    # lets peers receive it even if the saving client's socket just dropped.
    from app.collaboration.websocket import (
        broadcast_document_saved,
        broadcast_document_update,
    )

    await broadcast_document_update(canonical_session_id, document_state)
    await broadcast_document_saved(
        canonical_session_id,
        saved_at=result.saved_at,
        state_vector=result.state_vector,
        state_hash=result.state_hash,
        dirty=result.dirty,
        saved_by=current_user.username,
    )

    encoded_state_vector = base64.b64encode(result.state_vector).decode("ascii")

    return DocumentSaveResponse(
        session_id=session_id,
        size_bytes=result.size_bytes,
        saved_at=result.saved_at,
        state_vector=encoded_state_vector,
        state_hash=result.state_hash,
        dirty=result.dirty,
    )


@router.post("/{session_id}/members", status_code=status.HTTP_201_CREATED)
async def add_member_endpoint(
    session_id: uuid.UUID,
    body: AddMemberRequest,
    current_user: User = Depends(RoleChecker(min_role=RoleEnum.owner)),
    db: AsyncSession = Depends(get_db),
):
    """Add a user by ID or username to the session (owner only)."""
    user_id = await _resolve_member_user_id(db, body)
    try:
        member = await add_member(
            db=db,
            session_id=session_id,
            user_id=user_id,
            role=body.role,
        )
        return {
            "detail": "Member added",
            "user_id": str(member.user_id),
            "username": body.username,
            "role": member.role.value,
        }
    except SessionServiceError as error:
        _raise_http_error(error)


@router.patch("/{session_id}/members/{user_id}")
async def update_member_role_endpoint(
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    body: UpdateMemberRoleRequest,
    current_user: User = Depends(RoleChecker(min_role=RoleEnum.owner)),
    db: AsyncSession = Depends(get_db),
):
    """Change a member's role (owner only)."""
    from app.collaboration.websocket import (
        document_room_lifecycle,
        reauthenticate_member_connections,
    )

    async with document_room_lifecycle(str(session_id)):
        try:
            member = await update_member_role(db, session_id, user_id, body.role)
        except SessionServiceError as error:
            _raise_http_error(error)

        # Publish the in-memory fence before releasing the same gate used by
        # WebSocket updates, leaving no commit-to-revocation scheduling gap.
        await reauthenticate_member_connections(
            session_id,
            user_id,
            room_lock_held=True,
        )
    return {
        "detail": "Role updated",
        "user_id": str(user_id),
        "role": member.role.value,
    }


@router.delete("/{session_id}/members/{user_id}")
async def remove_member_endpoint(
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    current_user: User = Depends(RoleChecker(min_role=RoleEnum.owner)),
    db: AsyncSession = Depends(get_db),
):
    """Remove a member from the session (owner only)."""
    from app.collaboration.websocket import (
        document_room_lifecycle,
        revoke_member_connections,
    )

    async with document_room_lifecycle(str(session_id)):
        try:
            await remove_member(db, session_id, user_id)
        except SessionServiceError as error:
            _raise_http_error(error)
        await revoke_member_connections(
            session_id,
            user_id,
            room_lock_held=True,
        )
    return {"detail": "Member removed"}


@router.delete("/{session_id}")
async def close_session_endpoint(
    session_id: uuid.UUID,
    current_user: User = Depends(RoleChecker(min_role=RoleEnum.owner)),
    db: AsyncSession = Depends(get_db),
):
    """Close a session (owner only). Soft-deletes by setting is_active=False."""
    from app.collaboration.websocket import (
        close_session_connections,
        document_room_lifecycle,
    )

    async with document_room_lifecycle(str(session_id)):
        try:
            await close_session(db, session_id)
        except SessionServiceError as error:
            _raise_http_error(error)
        await close_session_connections(session_id, room_lock_held=True)
    return {"detail": "Session closed"}
