from datetime import date, datetime, timezone

import pytest

from planning.application.interfaces.travel_matrix_provider import TravelMatrix
from planning.domain.entities.coordinate import Coordinate
from planning.domain.entities.engineer import Engineer
from planning.domain.entities.job import Job
from planning.domain.entities.planning import PlanningConfig, PlanningInput
from planning.domain.enums import TransportType
from planning.infrastructure.adapters.planning_solver_ortools import (
    OrToolsPlanningSolver,
)


class _MatrixProvider:
    async def get_matrix(
        self,
        coordinates: list[Coordinate],
        profile: str,
        cache_ttl_days: int | None = None,
    ) -> TravelMatrix:
        size = len(coordinates)
        seconds = [
            [0 if origin == destination else 60 for destination in range(size)]
            for origin in range(size)
        ]
        distances = [
            [0 if origin == destination else 1_000 for destination in range(size)]
            for origin in range(size)
        ]
        return TravelMatrix(seconds, distances, profile, "TEST")


def _config() -> PlanningConfig:
    return PlanningConfig(
        id=1,
        version=1,
        sla_overdue_base=10_000,
        sla_overdue_per_day=1_000,
        sla_today=9_000,
        sla_tomorrow=4_000,
        sla_2_3_days=2_000,
        sla_later=500,
        skill_one_engineer=500,
        skill_two_engineers=250,
        equipment_one_unit=500,
        equipment_two_units=250,
        window_30=300,
        window_60=150,
        window_120=50,
        travel_cost_per_minute=1,
        solver_time_limit_sec=2,
        max_jobs_per_run=1000,
        travel_provider="TEST",
    )


@pytest.mark.parametrize("penalty_bits", [45, 64])
async def test_overflow_stops_without_changing_objective(
    penalty_bits: int,
) -> None:
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    jobs = [
        Job(
            id=index,
            sla_date=date(2026, 9, 21),
            duration_min=60,
            coordinate=Coordinate(58.0, 56.0 + index / 100),
            window_start_min=480,
            window_end_min=500,
            required_transport=TransportType.CAR,
            required_qualifications=frozenset(),
            required_equipment=frozenset(),
            created_at=now,
            drop_penalty=2**penalty_bits + index - 1,
        )
        for index in (1, 2)
    ]
    data = PlanningInput(
        project_id=1,
        planning_date=date(2026, 9, 21),
        timezone="UTC",
        config=_config(),
        jobs=jobs,
        engineers=[
            Engineer(
                id=1,
                transport_type=TransportType.CAR,
                coordinate=Coordinate(58.0, 56.0),
                shift_start_min=480,
                shift_end_min=600,
                qualifications=frozenset(),
            )
        ],
        equipment_units={},
        pre_unassigned=[],
        input_jobs_count=2,
        sla_critical_job_ids=frozenset({1, 2}),
        snapshot={},
    )

    with pytest.raises(RuntimeError, match="OBJECTIVE_RANGE_OVERFLOW"):
        await OrToolsPlanningSolver(_MatrixProvider()).solve(data)
