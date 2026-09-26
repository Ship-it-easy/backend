"""Seed the initial owner account for a fresh installation.

Revision ID: b42e5c18a906
Revises: 6c9e1a4d7b22

The password is a high-entropy bootstrap secret. Only its bcrypt hash belongs
in the repository; the original secret must be handed to the operator outside
the application database and changed after first use.
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "b42e5c18a906"
down_revision: str | None = "6c9e1a4d7b22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOGIN = "owner"
_SEED_USER_ID = UUID("5234596a-87fe-4578-a7b1-649f32da0545")
_PASSWORD_HASH = "$2b$12$nOhkToYziSS3zkZUn7r9SOz7R6AgDxlTC9dbCQbia.khQohQHDWOa"


def upgrade() -> None:
    # On a fresh database an earlier revision adds OWNER to userroleenum.
    # PostgreSQL requires that ALTER TYPE transaction to commit before an
    # INSERT may use the new enum value.
    with op.get_context().autocommit_block():
        pass

    connection = op.get_bind()
    existing = connection.execute(
        sa.text(
            "SELECT role::text FROM users WHERE lower(username) = :login FOR UPDATE"
        ),
        {"login": _LOGIN},
    ).scalar_one_or_none()
    if existing is not None:
        if existing != "OWNER":
            raise RuntimeError(
                "Cannot seed owner: username is already assigned to another role"
            )
        # Keep a manually provisioned owner's password and active state.
        return
    connection.execute(
        sa.text(
            """
            INSERT INTO users
                (id, username, password_hash, is_active, role, is_verified)
            VALUES
                (:id, :login, :password_hash, true, 'OWNER'::userroleenum, true)
            """
        ),
        {
            "id": _SEED_USER_ID,
            "login": _LOGIN,
            "password_hash": _PASSWORD_HASH,
        },
    )


def downgrade() -> None:
    # Account data is operational state. A rollback must not delete an owner
    # who may already have created projects and other users.
    pass
