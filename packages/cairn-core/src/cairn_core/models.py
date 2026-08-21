"""SQLAlchemy models. The DDL in the architecture doc is the source of truth;
this mirrors it, and Alembic migrations are generated from here.

Note that no model has an `updated_at` trigger and no row is ever mutated in
place except state transitions on `trajectories` and `approvals`. Steps are
append-only, because a replayable agent needs an append-only log.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from cairn_core.domain import ApprovalState, TrajectoryState


class Base(DeclarativeBase):
    pass


def _sql_tuple(enum_cls: type[StrEnum]) -> str:
    """Render an enum as a SQL IN-list so the CHECK constraint and the Python
    vocabulary cannot drift apart."""
    return "(" + ",".join(f"'{m.value}'" for m in enum_cls) + ")"


def _uuid_col() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Trajectory(Base):
    __tablename__ = "trajectories"

    id: Mapped[uuid.UUID] = _uuid_col()
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    answer: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column()
    #: Monotonic high-water mark. Once RESTRICTED it never comes back down.
    sensitivity: Mapped[str] = mapped_column(Text, nullable=False, default="public")
    tokens_local: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_cloud: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=Decimal("0"))
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    #: Set when the loop suspends on an approval so the resume sweeper knows
    #: which trajectory a webhook belongs to without a scan.
    waiting_approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    #: Owner pod, for crash recovery. NULL means unclaimed.
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    steps: Mapped[list[TrajectoryStep]] = relationship(
        back_populates="trajectory", cascade="all, delete-orphan", order_by="TrajectoryStep.seq"
    )

    __table_args__ = (
        CheckConstraint("state IN " + _sql_tuple(TrajectoryState), name="valid_state"),
        CheckConstraint(
            "sensitivity IN ('public','internal','restricted')",
            name="valid_sensitivity",
        ),
        Index("idx_traj_user_time", "user_id", started_at.desc()),
        # Partial: the resume sweeper runs every 30s and only cares about live
        # rows. A full index on a table that is 99% terminal states is waste.
        Index(
            "idx_traj_state",
            "state",
            postgresql_where=state.in_(["WAITING_APPROVAL", "EXECUTING"]),
        ),
    )


class TrajectoryStep(Base):
    __tablename__ = "trajectory_steps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trajectory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trajectories.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(Text)
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    artifact_id: Mapped[str | None] = mapped_column(Text)
    sensitivity: Mapped[str] = mapped_column(Text, nullable=False, default="public")
    model: Mapped[str | None] = mapped_column(Text)
    route: Mapped[str | None] = mapped_column(Text)
    route_reason: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    trajectory: Mapped[Trajectory] = relationship(back_populates="steps")

    __table_args__ = (
        UniqueConstraint("trajectory_id", "seq", name="uq_step_seq"),
        Index("idx_steps_traj", "trajectory_id", "seq"),
        CheckConstraint(
            "kind IN ('plan','tool_call','tool_result','llm_call','critique','compaction')",
            name="valid_kind",
        ),
    )


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = _uuid_col()
    trajectory_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: Deterministic over (action, semantic args). The unique index on this is
    #: what makes double-execution impossible rather than merely unlikely.
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default=ApprovalState.PENDING)
    requested_by: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by_email: Mapped[str | None] = mapped_column(Text)
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    approvals: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    denial_reason: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    slack_channel: Mapped[str | None] = mapped_column(Text)
    slack_ts: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_approval_idem"),
        CheckConstraint("state IN " + _sql_tuple(ApprovalState), name="valid_approval_state"),
        Index(
            "idx_approval_pending",
            "expires_at",
            postgresql_where=state == ApprovalState.PENDING.value,
        ),
    )


class AuditEvent(Base):
    """Append-only. Mirrored to S3 Object Lock by the exporter CronJob; the
    hourly reconciler diffs this against the actual mutation log."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    trajectory_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("idx_audit_at", at.desc()),
        Index("idx_audit_event", "event", at.desc()),
    )


class RunbookChunk(Base):
    __tablename__ = "runbook_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(Text, nullable=False)
    doc_version: Mapped[str] = mapped_column(Text, nullable=False)
    doc_title: Mapped[str | None] = mapped_column(Text)
    doc_url: Mapped[str | None] = mapped_column(Text)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    services: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    #: Author of the commit that introduced this content. The compensating
    #: control for T4 (poisoned runbook) is knowing who wrote it.
    source_author: Mapped[str | None] = mapped_column(Text)
    source_sha: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector(1024))
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', content)", persisted=True)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("doc_id", "doc_version", "chunk_index", name="uq_chunk"),
        Index(
            "idx_chunks_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("idx_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("idx_chunks_services", "services", postgresql_using="gin"),
    )


class TrajectoryEmbedding(Base):
    """Backs `find_similar_incidents`. Written when a trajectory reaches a
    terminal state, so it never indexes an in-flight investigation."""

    __tablename__ = "trajectory_embeddings"

    trajectory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trajectories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    services: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    embedding: Mapped[Any] = mapped_column(Vector(1024))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "idx_traj_emb_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class Artifact(Base):
    """Registry row for a payload parked in S3. The bytes are never in
    Postgres; this is the index that makes `fetch_artifact` cheap."""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    trajectory_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False, default="application/json")
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    row_count: Mapped[int | None] = mapped_column(Integer)
    sensitivity: Mapped[str] = mapped_column(Text, nullable=False, default="public")
    facets: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("idx_artifact_traj", "trajectory_id"),)
