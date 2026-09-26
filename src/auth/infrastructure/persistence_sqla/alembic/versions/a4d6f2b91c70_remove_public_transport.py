"""Retire public transport without giving former riders a car.

Revision ID: a4d6f2b91c70
Revises: b42e5c18a906
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4d6f2b91c70"
down_revision: str | None = "b42e5c18a906"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    affected_project_ids = connection.execute(
        sa.text(
            "SELECT project_id FROM engineers "
            "WHERE transport_type = 'PUBLIC_TRANSPORT' "
            "UNION SELECT project_id FROM work_types "
            "WHERE required_transport = 'PUBLIC_TRANSPORT'"
        )
    ).scalars().all()
    connection.execute(
        sa.text(
            "UPDATE engineers SET transport_type = 'NONE' "
            "WHERE transport_type = 'PUBLIC_TRANSPORT'"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE work_types SET required_transport = 'NONE' "
            "WHERE required_transport = 'PUBLIC_TRANSPORT'"
        )
    )
    connection.execute(
        sa.text(
            "ALTER TABLE engineers ADD CONSTRAINT ck_engineers_transport_valid "
            "CHECK (transport_type IN ('CAR', 'NONE', 'BICYCLE'))"
        )
    )
    connection.execute(
        sa.text(
            "ALTER TABLE work_types ADD CONSTRAINT ck_work_types_transport_valid "
            "CHECK (required_transport IS NULL OR "
            "required_transport IN ('CAR', 'NONE', 'BICYCLE'))"
        )
    )
    # The old published timetable can contain multimodal travel durations.
    # On backend startup the normal event recovery picks up these durable
    # system events and recalculates today's and future plans using road modes.
    for project_id in affected_project_ids:
        connection.execute(
            sa.text(
                "INSERT INTO planning_events "
                "(project_id, event_type, initiator, idempotency_key, state, "
                "event_payload) VALUES "
                "(:project_id, 'MANUAL', 'SYSTEM', "
                "'transport-retirement-a4d6f2b91c70', 'PENDING', "
                "'{\"reason\":\"PUBLIC_TRANSPORT_REMOVED\"}'::jsonb) "
                "ON CONFLICT (project_id, idempotency_key) DO NOTHING"
            ),
            {"project_id": project_id},
        )


def downgrade() -> None:
    connection = op.get_bind()
    # Preserve the event audit trail. A pending event can be processed safely
    # against the old code after downgrade, because it is a normal full replan.
    connection.execute(
        sa.text("ALTER TABLE work_types DROP CONSTRAINT ck_work_types_transport_valid")
    )
    connection.execute(
        sa.text("ALTER TABLE engineers DROP CONSTRAINT ck_engineers_transport_valid")
    )
    # The original transport choice cannot be reconstructed after migration.
