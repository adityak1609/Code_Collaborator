"""Session management endpoints — CRUD + member management."""

import uuid
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import RoleChecker, get_current_user
from app.database import get_db
from app.models import RoleEnum, User
from app.sessions.schemas import (
    AddMemberRequest,
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


@router.post("/{session_id}/members", status_code=status.HTTP_201_CREATED)
async def add_member_endpoint(
    session_id: uuid.UUID,
    body: AddMemberRequest,
    current_user: User = Depends(RoleChecker(min_role=RoleEnum.owner)),
    db: AsyncSession = Depends(get_db),
):
    """Add a user to the session (owner only)."""
    try:
        member = await add_member(
            db=db,
            session_id=session_id,
            user_id=body.user_id,
            role=body.role,
        )
        return {
            "detail": "Member added",
            "user_id": str(member.user_id),
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
    try:
        member = await update_member_role(db, session_id, user_id, body.role)
    except SessionServiceError as error:
        _raise_http_error(error)
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
    try:
        await remove_member(db, session_id, user_id)
    except SessionServiceError as error:
        _raise_http_error(error)
    return {"detail": "Member removed"}


@router.delete("/{session_id}")
async def close_session_endpoint(
    session_id: uuid.UUID,
    current_user: User = Depends(RoleChecker(min_role=RoleEnum.owner)),
    db: AsyncSession = Depends(get_db),
):
    """Close a session (owner only). Soft-deletes by setting is_active=False."""
    try:
        await close_session(db, session_id)
    except SessionServiceError as error:
        _raise_http_error(error)
    return {"detail": "Session closed"}
