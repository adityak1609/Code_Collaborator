"""Pydantic schemas for session and member endpoints."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

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
    user_id: uuid.UUID
    role: RoleEnum = RoleEnum.viewer


class UpdateMemberRoleRequest(BaseModel):
    role: RoleEnum


class MemberResponse(BaseModel):
    user_id: uuid.UUID
    username: str
    role: RoleEnum
    joined_at: datetime

    model_config = {"from_attributes": True}
