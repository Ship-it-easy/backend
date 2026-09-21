"""Persist CSV job imports and row validation results.

Revision ID: e37b19a5c200
Revises: d7f3a8b21c44
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e37b19a5c200"
down_revision = "d7f3a8b21c44"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "job_import_batches",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("source_bytes", sa.LargeBinary()),
        sa.Column("source_encoding", sa.String(16)),
        sa.Column("source_delimiter", sa.String(1)),
        sa.Column("validation_rules_version", sa.String(32)),
        sa.Column("address_provider_version", sa.String(32)),
        sa.Column("status", sa.String(32), nullable=False, server_default="UPLOADED"),
        sa.Column("stage", sa.String(64)),
        sa.Column("total_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("warning_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "warnings_acknowledged_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
        ),
        sa.Column("warnings_acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("planning_event_id", sa.BigInteger()),
        sa.Column("apply_idempotency_key", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("validation_started_at", sa.DateTime(timezone=True)),
        sa.Column("validation_finished_at", sa.DateTime(timezone=True)),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("source_file_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("technical_error_category", sa.String(64)),
        sa.Column(
            "package_issues", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
    )
    op.create_index(
        "uq_job_import_live_hash",
        "job_import_batches",
        ["project_id", "content_sha256"],
        unique=True,
        postgresql_where=sa.text("status <> 'EXPIRED'"),
    )
    op.create_table(
        "job_import_rows",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "batch_id",
            sa.BigInteger(),
            sa.ForeignKey("job_import_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("raw_required_values_json", postgresql.JSONB(), nullable=False),
        sa.Column("normalized_address", sa.Text()),
        sa.Column("canonical_address_key", sa.String(255)),
        sa.Column("latitude", sa.Numeric(9, 6)),
        sa.Column("longitude", sa.Numeric(9, 6)),
        sa.Column("sla_date", sa.Date()),
        sa.Column("time_window_start", sa.Time()),
        sa.Column("time_window_end", sa.Time()),
        sa.Column("work_type_id", sa.BigInteger(), sa.ForeignKey("work_types.id")),
        sa.Column("work_type_duration_min", sa.Integer()),
        sa.Column("severity", sa.String(16)),
        sa.Column(
            "issues_json", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column("created_job_id", sa.BigInteger(), sa.ForeignKey("jobs.id")),
        sa.UniqueConstraint("batch_id", "row_number"),
    )
    op.create_index(
        "ix_job_import_rows_batch", "job_import_rows", ["batch_id", "row_number"]
    )
    op.create_table(
        "job_import_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "batch_id",
            sa.BigInteger(),
            sa.ForeignKey("job_import_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_job_import_audit_batch", "job_import_audit", ["batch_id", "id"])
    op.add_column(
        "jobs",
        sa.Column(
            "import_batch_id", sa.BigInteger(), sa.ForeignKey("job_import_batches.id")
        ),
    )
    op.add_column("jobs", sa.Column("import_row_number", sa.Integer()))
    op.create_unique_constraint(
        "uq_jobs_import_origin", "jobs", ["import_batch_id", "import_row_number"]
    )


def downgrade():
    op.drop_index("ix_job_import_audit_batch", table_name="job_import_audit")
    op.drop_table("job_import_audit")
    op.drop_constraint("uq_jobs_import_origin", "jobs", type_="unique")
    op.drop_column("jobs", "import_row_number")
    op.drop_column("jobs", "import_batch_id")
    op.drop_index("ix_job_import_rows_batch", table_name="job_import_rows")
    op.drop_table("job_import_rows")
    op.drop_index("uq_job_import_live_hash", table_name="job_import_batches")
    op.drop_table("job_import_batches")
