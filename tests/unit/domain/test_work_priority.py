import pytest

from planning.domain.enums import WorkPriority
from planning.domain.priority import (
    work_priority,
    work_priority_bonus,
    work_priority_rank,
)


@pytest.mark.parametrize(
    ("priority", "rank", "bonus"),
    [
        (WorkPriority.CRITICAL, 0, 3_000_000),
        (WorkPriority.HIGH, 1, 2_000_000),
        (WorkPriority.MEDIUM, 2, 1_000_000),
        (WorkPriority.LOW, 3, 0),
    ],
)
def test_work_priority_order_and_bonus(
    priority: WorkPriority, rank: int, bonus: int
) -> None:
    assert work_priority_rank(priority) == rank
    assert work_priority_bonus(priority) == bonus


def test_legacy_snapshot_priority_becomes_low() -> None:
    assert work_priority("EMERGENCY") == WorkPriority.LOW
    assert work_priority(None) == WorkPriority.LOW
