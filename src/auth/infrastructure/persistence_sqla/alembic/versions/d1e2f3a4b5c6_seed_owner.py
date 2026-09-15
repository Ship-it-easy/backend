"""Seed the local owner account.

Revision ID: d1e2f3a4b5c6
Revises: c92a6f8d1e20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "c92a6f8d1e20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OWNER_ID = "2d3fb417-aa35-4b2a-96f7-ad63b30cc3b2"
OWNER_LOGIN = "owner"
OWNER_PASSWORD_HASH = "$2b$12$xvTCW8xXRHjYiB0uy6of3.YPRfw/GQSDK8d7Ew10Qw7d.2c9HXyc."


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO users (
            id, username, password_hash, is_active, role, is_verified,
            project_id, engineer_id
        )
        VALUES (
            '{OWNER_ID}'::uuid,
            '{OWNER_LOGIN}',
            '{OWNER_PASSWORD_HASH}',
            TRUE,
            'OWNER',
            TRUE,
            NULL,
            NULL
        )
        ON CONFLICT (lower(username)) DO UPDATE SET
            password_hash = EXCLUDED.password_hash,
            is_active = TRUE,
            role = 'OWNER',
            is_verified = TRUE,
            project_id = NULL,
            engineer_id = NULL
        """
    )


def downgrade() -> None:
    op.execute(
        f"DELETE FROM users WHERE id = '{OWNER_ID}'::uuid"
    )
