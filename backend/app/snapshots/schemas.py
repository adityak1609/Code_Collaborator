"""Snapshot request and response schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class SnapshotCreate(BaseModel):
    label: str = Field(min_length=1, max_length=255)

    model_config = {"str_strip_whitespace": True}


class SnapshotResponse(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    created_by: uuid.UUID | None
    label: str
    size_bytes: int
    created_at: datetime

    model_config = {"from_attributes": True}


class SnapshotHistoryResponse(BaseModel):
    items: list[SnapshotResponse]
    total: int
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class SnapshotRestoreResponse(BaseModel):
    snapshot: SnapshotResponse
    update_size_bytes: int
