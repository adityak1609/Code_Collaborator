"""Pydantic schemas for session and member endpoints."""

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, Field, model_validator

from app.models import LanguageEnum, RoleEnum

# ── Session schemas ───────────────────────────────────────────


class SessionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    language: LanguageEnum


class SessionResponse(BaseModel):
    id: uuid.UUID
    name: str
    language: LanguageEnum
    owner_id: uuid.UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SessionDetailResponse(SessionResponse):
    members: list["MemberResponse"]


class DocumentSaveResponse(BaseModel):
    session_id: uuid.UUID
    size_bytes: int
    saved_at: datetime
    state_vector: str
    state_hash: str
    dirty: bool


# ── Member schemas ────────────────────────────────────────────


class AddMemberRequest(BaseModel):
    user_id: uuid.UUID | None = None
    username: str | None = Field(default=None, min_length=3, max_length=64)
    role: RoleEnum = RoleEnum.viewer

    @model_validator(mode="after")
    def require_one_user_identifier(self) -> Self:
        if (self.user_id is None) == (self.username is None):
            raise ValueError("Provide exactly one of user_id or username")
        return self


class UpdateMemberRoleRequest(BaseModel):
    role: RoleEnum


class MemberResponse(BaseModel):
    user_id: uuid.UUID
    username: str
    role: RoleEnum
    joined_at: datetime

    model_config = {"from_attributes": True}
