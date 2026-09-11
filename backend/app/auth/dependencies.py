"""FastAPI dependencies for authentication and role-based access control.

- get_current_user: extracts and validates JWT from Authorization header.
- RoleChecker: callable dependency that verifies the user's role in a session.
"""

import uuid

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import decode_token
from app.database import get_db
from app.models import ROLE_RANK, RoleEnum, Session, SessionMember, User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Decode JWT and return the authenticated User, or raise 401."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
        subject = payload.get("sub")
        if not isinstance(subject, str):
            raise ValueError("Token subject must be a UUID string")
        user_id = uuid.UUID(subject)
    except (JWTError, TypeError, ValueError) as exc:
        raise credentials_exception from exc

    user = await db.get(User, user_id)
    if user is None:
        raise credentials_exception
    return user


class RoleChecker:
    """Dependency that enforces a minimum role for a session.

    Usage in a route:
        @router.post("/{session_id}/run")
        async def run(
            session_id: UUID,
            user: User = Depends(RoleChecker(min_role=RoleEnum.editor)),
        ): ...
    """

    def __init__(self, min_role: RoleEnum) -> None:
        self.min_role = min_role

    async def __call__(
        self,
        session_id: uuid.UUID,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        session = await db.get(Session, session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found",
            )

        if not session.is_active:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Session is closed",
            )

        # Look up the user's membership only after validating the session itself.
        result = await db.execute(
            select(SessionMember).where(
                SessionMember.session_id == session_id,
                SessionMember.user_id == current_user.id,
            )
        )
        membership = result.scalar_one_or_none()

        if membership is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not a member of this session",
            )

        # The owner role is canonical only when it agrees with Session.owner_id.
        # This prevents an inconsistent/legacy extra `owner` membership from
        # gaining session-management permissions.
        if self.min_role is RoleEnum.owner and session.owner_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Requires the canonical session owner",
            )

        if ROLE_RANK[membership.role] < ROLE_RANK[self.min_role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Requires role '{self.min_role.value}' or higher. "
                    f"You have '{membership.role.value}'."
                ),
            )

        return current_user


async def get_ws_user(
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract user from query-param token for WebSocket connections.

    WebSockets can't send Authorization headers from browsers,
    so the token is passed as ?token=<jwt>.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid WebSocket token",
    )
    try:
        payload = decode_token(token)
        subject = payload.get("sub")
        if not isinstance(subject, str):
            raise ValueError("Token subject must be a UUID string")
        user_id = uuid.UUID(subject)
    except (JWTError, TypeError, ValueError) as exc:
        raise credentials_exception from exc

    user = await db.get(User, user_id)
    if user is None:
        raise credentials_exception
    return user
