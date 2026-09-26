import pytest
from pydantic import ValidationError

from planning.presentation.http.project.schemas import (
    JobCreate,
    WorkTypeCreate,
    WorkTypePatch,
)


@pytest.mark.parametrize("priority", ["CRITICAL", "HIGH", "MEDIUM", "LOW"])
def test_work_type_accepts_every_priority(priority: str) -> None:
    value = WorkTypeCreate(
        name="Диагностика",
        priority=priority,
        default_service_duration_min=30,
    )

    assert value.priority == priority


def test_job_does_not_accept_its_own_priority() -> None:
    with pytest.raises(ValidationError):
        JobCreate(
            address="Пермь, Ленина, 1",
            sla_date="2026-09-22",
            work_type_id=1,
            priority="CRITICAL",
        )


def test_work_type_patch_can_clear_required_transport() -> None:
    assert WorkTypePatch(required_transport=None).update_values() == {
        "required_transport": None
    }
    assert WorkTypePatch(name="Диагностика").update_values() == {
        "name": "Диагностика"
    }
    assert WorkTypePatch(required_transport="CAR").update_values() == {
        "required_transport": "CAR"
    }
