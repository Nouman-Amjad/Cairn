"""Initial schema: trajectories, steps, approvals, audit, runbooks, artifacts.

Revision ID: 0001
Revises:
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TRAJECTORY_STATES = (
    "PLANNING",
    "EXECUTING",
    "WAITING_APPROVAL",
    "SYNTHESIZING",
    "CRITIQUING",
    "COMPLETE",
    "PARTIAL",
    "FAILED",
    "ABANDONED",
)
APPROVAL_STATES = ("PENDING", "APPROVED", "DENIED", "EXECUTED", "EXPIRED", "FAILED")
STEP_KINDS = ("plan", "tool_call", "tool_result", "llm_call", "critique", "compaction")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ",".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "trajectories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("plan", postgresql.JSONB()),
        sa.Column("answer", sa.Text()),
        sa.Column("confidence", sa.Float()),
        sa.Column("sensitivity", sa.Text(), nullable=False, server_default="public"),
        sa.Column("tokens_local", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_cloud", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("waiting_approval_id", postgresql.UUID(as_uuid=True)),
        sa.Column("lease_owner", sa.Text()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(_in_list("state", TRAJECTORY_STATES), name="valid_state"),
        sa.CheckConstraint(
            "sensitivity IN ('public','internal','restricted')",
            name="valid_sensitivity",
        ),
    )
    op.create_index("idx_traj_user_time", "trajectories", ["user_id", sa.text("started_at DESC")])
    op.create_index(
        "idx_traj_state",
        "trajectories",
        ["state"],
        postgresql_where=sa.text("state IN ('WAITING_APPROVAL','EXECUTING')"),
    )

    op.create_table(
        "trajectory_steps",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "trajectory_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trajectories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text()),
        sa.Column("input", postgresql.JSONB()),
        sa.Column("output", postgresql.JSONB()),
        sa.Column("artifact_id", sa.Text()),
        sa.Column("sensitivity", sa.Text(), nullable=False, server_default="public"),
        sa.Column("model", sa.Text()),
        sa.Column("route", sa.Text()),
        sa.Column("route_reason", sa.Text()),
        sa.Column("tokens_in", sa.Integer()),
        sa.Column("tokens_out", sa.Integer()),
        sa.Column("cost_usd", sa.Numeric(10, 6)),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("error", postgresql.JSONB()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("trajectory_id", "seq", name="uq_step_seq"),
        sa.CheckConstraint(_in_list("kind", STEP_KINDS), name="valid_kind"),
    )
    op.create_index("idx_steps_traj", "trajectory_steps", ["trajectory_id", "seq"])

    op.create_table(
        "approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("trajectory_id", postgresql.UUID(as_uuid=True)),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("args", postgresql.JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("requested_by_email", sa.Text()),
        sa.Column("required_approvals", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("approvals", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("decided_by", sa.Text()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("denial_reason", sa.Text()),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("slack_channel", sa.Text()),
        sa.Column("slack_ts", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # This unique index is the whole idempotency guarantee. Without it,
        # "we compute a key" is a comment, not a control.
        sa.UniqueConstraint("idempotency_key", name="uq_approval_idem"),
        sa.CheckConstraint(_in_list("state", APPROVAL_STATES), name="valid_approval_state"),
    )
    op.create_index(
        "idx_approval_pending",
        "approvals",
        ["expires_at"],
        postgresql_where=sa.text("state = 'PENDING'"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text()),
        sa.Column("trajectory_id", postgresql.UUID(as_uuid=True)),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True)),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index("idx_audit_at", "audit_events", [sa.text("at DESC")])
    op.create_index("idx_audit_event", "audit_events", ["event", sa.text("at DESC")])
    # The audit log is append-only at the database level, not merely by
    # convention. The application role cannot rewrite history.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION cairn_audit_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER audit_no_mutate BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION cairn_audit_append_only()"
    )

    op.create_table(
        "runbook_chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("doc_id", sa.Text(), nullable=False),
        sa.Column("doc_version", sa.Text(), nullable=False),
        sa.Column("doc_title", sa.Text()),
        sa.Column("doc_url", sa.Text()),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column(
            "heading_path",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("services", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("source_author", sa.Text()),
        sa.Column("source_sha", sa.Text()),
        sa.Column("embedding", Vector(1024)),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', content)", persisted=True),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("doc_id", "doc_version", "chunk_index", name="uq_chunk"),
    )
    op.create_index(
        "idx_chunks_hnsw",
        "runbook_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("idx_chunks_tsv", "runbook_chunks", ["tsv"], postgresql_using="gin")
    op.create_index("idx_chunks_services", "runbook_chunks", ["services"], postgresql_using="gin")

    op.create_table(
        "trajectory_embeddings",
        sa.Column(
            "trajectory_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trajectories.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("services", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("embedding", Vector(1024)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "idx_traj_emb_hnsw",
        "trajectory_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("trajectory_id", postgresql.UUID(as_uuid=True)),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False, server_default="application/json"),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("row_count", sa.Integer()),
        sa.Column("sensitivity", sa.Text(), nullable=False, server_default="public"),
        sa.Column("facets", postgresql.JSONB()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("idx_artifact_traj", "artifacts", ["trajectory_id"])


def downgrade() -> None:
    op.drop_table("artifacts")
    op.drop_table("trajectory_embeddings")
    op.drop_table("runbook_chunks")
    op.execute("DROP TRIGGER IF EXISTS audit_no_mutate ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS cairn_audit_append_only()")
    op.drop_table("audit_events")
    op.drop_table("approvals")
    op.drop_table("trajectory_steps")
    op.drop_table("trajectories")
