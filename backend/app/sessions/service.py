"""Session business logic — CRUD and member management."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import LanguageEnum, RoleEnum, Session, SessionMember, User


class SessionServiceError(Exception):
    """Base class for expected session-domain failures."""


class SessionResourceNotFoundError(SessionServiceError):
    """A requested session, user, or member does not exist."""


class SessionConflictError(SessionServiceError):
    """An operation conflicts with session membership invariants."""


class SessionInactiveError(SessionServiceError):
    """An operation targets a session that has already been closed."""


async def _get_active_session(db: AsyncSession, session_id: uuid.UUID) -> Session:
    session = await db.get(Session, session_id)
    if session is None:
        raise SessionResourceNotFoundError("Session not found")
    if not session.is_active:
        raise SessionInactiveError("Session is closed")
    return session


async def create_session(
    db: AsyncSession,
    name: str,
    language: LanguageEnum,
    owner: User,
) -> Session:
    """Create a new session and add the creator as owner."""
    session = Session(
        name=name,
        language=language,
        owner_id=owner.id,
    )
    db.add(session)
    await db.flush()

    # Add creator as owner member
    member = SessionMember(
        session_id=session.id,
        user_id=owner.id,
        role=RoleEnum.owner,
    )
    db.add(member)
    await db.commit()
    await db.refresh(session)
    return session


async def list_user_sessions(db: AsyncSession, user_id: uuid.UUID) -> list[Session]:
    """List all sessions the user is a member of."""
    result = await db.execute(
        select(Session)
        .join(SessionMember)
        .where(
            SessionMember.user_id == user_id,
            Session.is_active.is_(True),
        )
        .order_by(Session.updated_at.desc())
    )
    return list(result.scalars().all())


async def get_session_detail(db: AsyncSession, session_id: uuid.UUID) -> Session | None:
    """Get a session with its members eagerly loaded."""
    result = await db.execute(
        select(Session)
        .options(selectinload(Session.members).selectinload(SessionMember.user))
        .where(Session.id == session_id)
    )
    return result.scalar_one_or_none()


async def add_member(
    db: AsyncSession,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    role: RoleEnum,
) -> SessionMember:
    """Add a user to a session with the specified role."""
    session = await _get_active_session(db, session_id)

    if role is RoleEnum.owner:
        raise SessionConflictError(
            "A session can have only one owner; ownership transfer is not supported"
        )

    if user_id == session.owner_id:
        raise SessionConflictError("The canonical owner membership cannot be replaced")

    # Check user exists
    user = await db.get(User, user_id)
    if user is None:
        raise SessionResourceNotFoundError("User not found")

    # Check not already a member
    existing = await db.execute(
        select(SessionMember).where(
            SessionMember.session_id == session_id,
            SessionMember.user_id == user_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise SessionConflictError("User is already a member of this session")

    member = SessionMember(
        session_id=session_id,
        user_id=user_id,
        role=role,
    )
    db.add(member)
    await db.commit()
    await db.refresh(member)
    return member


async def update_member_role(
    db: AsyncSession,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    new_role: RoleEnum,
) -> SessionMember:
    """Change a member's role while preserving the single-owner invariant."""
    session = await _get_active_session(db, session_id)

    if user_id == session.owner_id and new_role is not RoleEnum.owner:
        raise SessionConflictError("The canonical session owner cannot be demoted")

    if user_id != session.owner_id and new_role is RoleEnum.owner:
        raise SessionConflictError(
            "A session can have only one owner; ownership transfer is not supported"
        )

    result = await db.execute(
        select(SessionMember).where(
            SessionMember.session_id == session_id,
            SessionMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise SessionResourceNotFoundError("Member not found")

    if member.role is new_role:
        return member

    member.role = new_role
    await db.commit()
    await db.refresh(member)
    return member


async def remove_member(
    db: AsyncSession,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Remove a member without allowing the canonical owner to be removed."""
    session = await _get_active_session(db, session_id)
    if user_id == session.owner_id:
        raise SessionConflictError("The canonical session owner cannot be removed")

    result = await db.execute(
        select(SessionMember).where(
            SessionMember.session_id == session_id,
            SessionMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise SessionResourceNotFoundError("Member not found")

    await db.delete(member)
    await db.commit()


async def close_session(db: AsyncSession, session_id: uuid.UUID) -> Session:
    """Mark a session as inactive (soft delete)."""
    session = await _get_active_session(db, session_id)

    session.is_active = False
    await db.commit()
    await db.refresh(session)
    return session
