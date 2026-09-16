"""Snapshot persistence and lookup rules."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Snapshot


class SnapshotNotFoundError(Exception):
    """The requested snapshot does not belong to the session."""


async def create_snapshot(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    created_by: uuid.UUID,
    label: str,
    document_state: bytes,
) -> Snapshot:
    snapshot = Snapshot(
        session_id=session_id,
        created_by=created_by,
        label=label.strip(),
        document_state=document_state,
        size_bytes=len(document_state),
    )
    db.add(snapshot)
    await db.commit()
    await db.refresh(snapshot)
    return snapshot


async def list_snapshots(
    db: AsyncSession,
    session_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[Snapshot], int]:
    total_result = await db.execute(
        select(func.count())
        .select_from(Snapshot)
        .where(Snapshot.session_id == session_id)
    )
    result = await db.execute(
        select(Snapshot)
        .where(Snapshot.session_id == session_id)
        .order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), int(total_result.scalar_one())


async def get_snapshot(
    db: AsyncSession,
    session_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> Snapshot:
    result = await db.execute(
        select(Snapshot).where(
            Snapshot.id == snapshot_id,
            Snapshot.session_id == session_id,
        )
    )
    snapshot = result.scalar_one_or_none()
    if snapshot is None:
        raise SnapshotNotFoundError("Snapshot not found")
    return snapshot
