"""Allow a dispatcher to access multiple projects.

Revision ID: 7c4a1d9e2b60
Revises: 1b8e6d4a2c70
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7c4a1d9e2b60"
down_revision: Union[str, None] = "1b8e6d4a2c70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dispatcher_projects",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "assigned_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_dispatcher_projects_project_id",
        "dispatcher_projects",
        ["project_id"],
    )
    op.execute(
        """
        INSERT INTO dispatcher_projects (user_id, project_id)
        SELECT id, project_id
        FROM users
        WHERE role::text = 'DISPATCHER' AND project_id IS NOT NULL
        ON CONFLICT (user_id, project_id) DO NOTHING
        """
    )
    # dispatcher_projects is the authorization source of truth. Keep the
    # existing project_id as a temporary single-project compatibility mirror;
    # application writes clear it as soon as a dispatcher has zero or several
    # assignments.


def downgrade() -> None:
    # A legacy dispatcher can represent only one project. Pick the oldest
    # assignment deterministically if the upgraded system contains several.
    op.execute(
        """
        UPDATE users AS target
        SET project_id = source.project_id
        FROM (
            SELECT DISTINCT ON (user_id) user_id, project_id
            FROM dispatcher_projects
            ORDER BY user_id, assigned_at, project_id
        ) AS source
        WHERE target.id = source.user_id AND target.role::text = 'DISPATCHER'
        """
    )
    op.drop_index(
        "ix_dispatcher_projects_project_id",
        table_name="dispatcher_projects",
    )
    op.drop_table("dispatcher_projects")
