"""Request and response schemas for execution endpoints."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models import ExecutionStatus, LanguageEnum


class ExecutionResponse(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    triggered_by: uuid.UUID | None
    status: ExecutionStatus
    code: str
    language: LanguageEnum
    stdout: str | None
    stderr: str | None
    exit_code: int | None
    elapsed_ms: int | None
    created_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class ExecutionHistoryResponse(BaseModel):
    items: list[ExecutionResponse]
    total: int
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
