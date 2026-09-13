"""Focused tests for authentication and session ownership invariants."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status
from pydantic import ValidationError

from app.auth import dependencies as auth_dependencies
from app.auth.dependencies import RoleChecker
from app.models import RoleEnum
from app.sessions.router import (
    _raise_http_error,
    _require_current_editor,
    _resolve_member_user_id,
)
from app.sessions.schemas import AddMemberRequest
from app.sessions.service import (
    SessionConflictError,
    SessionInactiveError,
    SessionResourceNotFoundError,
    add_member,
    remove_member,
    update_member_role,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolver",
    [auth_dependencies.get_current_user, auth_dependencies.get_ws_user],
)
async def test_malformed_token_subject_returns_401(monkeypatch, resolver):
    monkeypatch.setattr(
        auth_dependencies,
        "decode_token",
        lambda _token: {"sub": "not-a-uuid"},
    )
    db = MagicMock()
    db.get = AsyncMock()

    with pytest.raises(HTTPException) as caught:
        await resolver(token="token", db=db)

    assert caught.value.status_code == status.HTTP_401_UNAUTHORIZED
    db.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session", "expected_status", "expected_detail"),
    [
        (None, status.HTTP_404_NOT_FOUND, "Session not found"),
        (
            SimpleNamespace(is_active=False),
            status.HTTP_410_GONE,
            "Session is closed",
        ),
    ],
)
async def test_role_checker_rejects_missing_or_closed_sessions(
    session, expected_status, expected_detail
):
    db = MagicMock()
    db.get = AsyncMock(return_value=session)
    db.execute = AsyncMock()
    checker = RoleChecker(min_role=RoleEnum.viewer)

    with pytest.raises(HTTPException) as caught:
        await checker(
            session_id=uuid.uuid4(),
            current_user=SimpleNamespace(id=uuid.uuid4()),
            db=db,
        )

    assert caught.value.status_code == expected_status
    assert caught.value.detail == expected_detail
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_noncanonical_owner_membership_cannot_manage_session():
    canonical_owner_id = uuid.uuid4()
    extra_owner = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(
        is_active=True,
        owner_id=canonical_owner_id,
    )
    query_result = MagicMock()
    query_result.scalar_one_or_none.return_value = SimpleNamespace(role=RoleEnum.owner)
    db = MagicMock()
    db.get = AsyncMock(return_value=session)
    db.execute = AsyncMock(return_value=query_result)

    with pytest.raises(HTTPException) as caught:
        await RoleChecker(min_role=RoleEnum.owner)(
            session_id=uuid.uuid4(),
            current_user=extra_owner,
            db=db,
        )

    assert caught.value.status_code == status.HTTP_403_FORBIDDEN
    assert caught.value.detail == "Requires the canonical session owner"


@pytest.mark.asyncio
async def test_cannot_add_a_second_owner():
    session_id = uuid.uuid4()
    session = SimpleNamespace(is_active=True, owner_id=uuid.uuid4())
    db = MagicMock()
    db.get = AsyncMock(return_value=session)
    db.execute = AsyncMock()

    with pytest.raises(SessionConflictError, match="only one owner"):
        await add_member(
            db=db,
            session_id=session_id,
            user_id=uuid.uuid4(),
            role=RoleEnum.owner,
        )

    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_canonical_owner_cannot_be_demoted_or_removed():
    session_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    session = SimpleNamespace(is_active=True, owner_id=owner_id)
    db = MagicMock()
    db.get = AsyncMock(return_value=session)
    db.execute = AsyncMock()

    with pytest.raises(SessionConflictError, match="cannot be demoted"):
        await update_member_role(
            db=db,
            session_id=session_id,
            user_id=owner_id,
            new_role=RoleEnum.editor,
        )

    with pytest.raises(SessionConflictError, match="cannot be removed"):
        await remove_member(
            db=db,
            session_id=session_id,
            user_id=owner_id,
        )

    db.execute.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (SessionResourceNotFoundError("missing"), status.HTTP_404_NOT_FOUND),
        (SessionConflictError("conflict"), status.HTTP_409_CONFLICT),
        (SessionInactiveError("closed"), status.HTTP_410_GONE),
    ],
)
def test_session_service_errors_have_stable_http_statuses(error, expected_status):
    with pytest.raises(HTTPException) as caught:
        _raise_http_error(error)

    assert caught.value.status_code == expected_status
    assert caught.value.detail == str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "expected_status", "expected_detail"),
    [
        (None, status.HTTP_404_NOT_FOUND, "Session not found"),
        (
            SimpleNamespace(is_active=False, role=RoleEnum.editor),
            status.HTTP_410_GONE,
            "Session is closed",
        ),
        (
            SimpleNamespace(is_active=True, role=None),
            status.HTTP_403_FORBIDDEN,
            "Insufficient permissions",
        ),
        (
            SimpleNamespace(is_active=True, role=RoleEnum.viewer),
            status.HTTP_403_FORBIDDEN,
            "Insufficient permissions",
        ),
    ],
)
async def test_current_editor_recheck_rejects_stale_authority(
    row, expected_status, expected_detail
):
    result = MagicMock()
    result.one_or_none.return_value = row
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    with pytest.raises(HTTPException) as caught:
        await _require_current_editor(db, uuid.uuid4(), uuid.uuid4())

    assert caught.value.status_code == expected_status
    assert caught.value.detail == expected_detail


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [RoleEnum.editor, RoleEnum.owner])
async def test_current_editor_recheck_accepts_write_roles(role):
    result = MagicMock()
    result.one_or_none.return_value = SimpleNamespace(is_active=True, role=role)
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    await _require_current_editor(db, uuid.uuid4(), uuid.uuid4())

    db.execute.assert_awaited_once()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"user_id": str(uuid.uuid4()), "username": "ada"},
    ],
)
def test_add_member_request_requires_one_identifier(payload):
    with pytest.raises(ValidationError, match="exactly one"):
        AddMemberRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_member_invitation_resolves_username():
    user_id = uuid.uuid4()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user_id
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    resolved = await _resolve_member_user_id(
        db,
        AddMemberRequest(username="ada", role=RoleEnum.editor),
    )

    assert resolved == user_id
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_member_invitation_rejects_unknown_username():
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    with pytest.raises(HTTPException) as caught:
        await _resolve_member_user_id(db, AddMemberRequest(username="missing"))

    assert caught.value.status_code == status.HTTP_404_NOT_FOUND
    assert caught.value.detail == "User not found"
