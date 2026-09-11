"""SQLAlchemy ORM models — all five core tables.

Uses SQLAlchemy 2.0 Mapped/mapped_column syntax with explicit
naming conventions for constraints (required for Alembic).
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)

# ── Naming convention for all constraints ─────────────────────
# Ensures Alembic generates deterministic constraint names.
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=convention)


# ── Enums ─────────────────────────────────────────────────────


class RoleEnum(StrEnum):
    viewer = "viewer"
    editor = "editor"
    owner = "owner"


class LanguageEnum(StrEnum):
    python = "python"
    cpp = "cpp"
    javascript = "javascript"


class ExecutionStatus(StrEnum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


# ── Valid execution state transitions ─────────────────────────

VALID_TRANSITIONS: dict[ExecutionStatus, set[ExecutionStatus]] = {
    ExecutionStatus.QUEUED: {ExecutionStatus.STARTING, ExecutionStatus.CANCELLED},
    ExecutionStatus.STARTING: {ExecutionStatus.RUNNING, ExecutionStatus.FAILED},
    ExecutionStatus.RUNNING: {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.TIMEOUT,
        ExecutionStatus.CANCELLED,
    },
    # Terminal states — no valid outgoing transitions
    ExecutionStatus.COMPLETED: set(),
    ExecutionStatus.FAILED: set(),
    ExecutionStatus.TIMEOUT: set(),
    ExecutionStatus.CANCELLED: set(),
}

ROLE_RANK: dict[RoleEnum, int] = {
    RoleEnum.viewer: 0,
    RoleEnum.editor: 1,
    RoleEnum.owner: 2,
}


# ── Models ────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    owned_sessions: Mapped[list["Session"]] = relationship(
        back_populates="owner", lazy="selectin"
    )
    memberships: Mapped[list["SessionMember"]] = relationship(
        back_populates="user", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<User {self.username}>"


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128))
    language: Mapped[LanguageEnum] = mapped_column(
        Enum(LanguageEnum, name="language_enum")
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Latest CRDT state — binary snapshot for persistence/recovery
    yjs_state: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True, default=None
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    owner: Mapped["User"] = relationship(back_populates="owned_sessions")
    members: Mapped[list["SessionMember"]] = relationship(
        back_populates="session", lazy="selectin", cascade="all, delete-orphan"
    )
    executions: Mapped[list["Execution"]] = relationship(
        back_populates="session", lazy="noload", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list["Snapshot"]] = relationship(
        back_populates="session", lazy="noload", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Session {self.name} ({self.language.value})>"


class SessionMember(Base):
    __tablename__ = "session_members"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[RoleEnum] = mapped_column(
        Enum(RoleEnum, name="role_enum"), default=RoleEnum.viewer
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    session: Mapped["Session"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="memberships")

    def __repr__(self) -> str:
        return f"<SessionMember user={self.user_id} role={self.role.value}>"


class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    triggered_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[ExecutionStatus] = mapped_column(
        Enum(ExecutionStatus, name="execution_status_enum"),
        default=ExecutionStatus.QUEUED,
    )
    code: Mapped[str] = mapped_column(Text)
    language: Mapped[LanguageEnum] = mapped_column(
        Enum(LanguageEnum, name="language_enum", create_type=False)
    )
    stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    session: Mapped["Session"] = relationship(back_populates="executions")

    def transition_to(self, new_status: ExecutionStatus) -> None:
        """Validate and apply a state transition.

        Raises ValueError if the transition is not permitted.
        """
        allowed = VALID_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Invalid transition: {self.status.value} → {new_status.value}. "
                f"Allowed: {[s.value for s in allowed]}"
            )
        self.status = new_status

    def __repr__(self) -> str:
        return f"<Execution {self.id} [{self.status.value}]>"


class Snapshot(Base):
    __tablename__ = "snapshots"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    label: Mapped[str] = mapped_column(String(255))
    document_state: Mapped[bytes] = mapped_column(LargeBinary)
    size_bytes: Mapped[int] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    session: Mapped["Session"] = relationship(back_populates="snapshots")

    def __repr__(self) -> str:
        return f"<Snapshot {self.label} ({self.size_bytes}B)>"
