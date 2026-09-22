"""Контракт дневного расчёта и воспроизводимый пример из документа для демо."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from planning.application.errors import SolverNoFeasibleSolution
from planning.application.interfaces.travel_matrix_provider import TravelMatrix
from planning.application.validators.planning_result import PlanningValidator
from planning.domain.entities.job import UnassignedJob
from planning.domain.enums import ReasonCode, TransportType
from planning.infrastructure.adapters.planning_solver_ortools import (
    OrToolsPlanningSolver,
)
from tests.unit.infrastructure.test_routing_regressions import (
    Provider,
    data,
    engineer,
    job,
)


def demo_input():
    """Анна: автомобиль и оптика. Борис: пешком и настройка оборудования."""
    return data(
        [
            job(
                1,
                duration_min=40,
                window_end_min=540,
                required_transport=TransportType.CAR,
                required_qualifications=frozenset({1}),
                required_equipment=frozenset({1}),
            ),
            job(
                2,
                duration_min=30,
                window_start_min=570,
                window_end_min=600,
                required_transport=TransportType.CAR,
                required_qualifications=frozenset({1}),
                required_equipment=frozenset({1}),
            ),
            job(
                3,
                duration_min=20,
                window_end_min=555,
                required_qualifications=frozenset({2}),
            ),
        ],
        [
            engineer(1, qualifications=frozenset({1}), shift_end_min=720),
            engineer(
                2,
                transport_type=TransportType.NONE,
                qualifications=frozenset({2}),
                shift_end_min=600,
            ),
        ],
        equipment_units={1: 1},
    )


class DemoProvider:
    async def get_matrix(self, coordinates, profile, cache_ttl_days=None):
        minutes = 10 if profile == "auto" else 5
        meters = 3000 if profile == "auto" else 400
        return TravelMatrix(
            [[0 if a == b else minutes * 60 for b in coordinates] for a in coordinates],
            [[0 if a == b else meters for b in coordinates] for a in coordinates],
            profile,
            "DEMO_TEST",
        )


async def test_demo_use_case_has_exact_timetable_and_profile_specific_travel():
    planning = demo_input()
    result = await OrToolsPlanningSolver(DemoProvider()).solve(planning)
    assert PlanningValidator().validate(planning, result) == []
    assert result.unassigned == []
    anna, boris = result.routes
    assert (anna.engineer_id, boris.engineer_id) == (1, 2)
    assert [item.job_id for item in anna.jobs] == [1, 2]
    assert [item.job_id for item in boris.jobs] == [3]
    assert [item.planned_start.strftime("%H:%M") for item in anna.jobs] == [
        "08:10",
        "09:30",
    ]
    assert [item.planned_finish.strftime("%H:%M") for item in anna.jobs] == [
        "08:50",
        "10:00",
    ]
    assert anna.jobs[1].planned_arrival.strftime("%H:%M") == "09:00"
    assert anna.total_waiting_min == 30
    assert anna.total_travel_min == 20
    assert anna.total_distance_meters == 6000
    assert anna.equipment_type_ids == {1}
    assert boris.jobs[0].planned_start.strftime("%H:%M") == "08:05"
    assert boris.planned_finish.strftime("%H:%M") == "08:25"
    assert boris.total_distance_meters == 400


async def test_window_limits_start_not_finish_and_no_return_trip_is_required():
    planning = data(
        [job(1, window_start_min=500, window_end_min=500, duration_min=60)],
        [engineer(shift_end_min=560)],
    )
    result = await OrToolsPlanningSolver(Provider([[0, None], [20, 0]])).solve(planning)
    assert PlanningValidator().validate(planning, result) == []
    visit = result.routes[0].jobs[0]
    assert visit.planned_start.strftime("%H:%M") == "08:20"
    assert visit.planned_finish.strftime("%H:%M") == "09:20"


async def test_mandatory_job_cannot_be_dropped_to_serve_higher_penalty_optional_job():
    planning = data(
        [
            job(1, mandatory=True, duration_min=50, drop_penalty=0),
            job(2, duration_min=50, drop_penalty=1_000_000),
        ],
        [engineer(shift_end_min=540)],
    )
    result = await OrToolsPlanningSolver(Provider()).solve(planning)
    assert PlanningValidator().validate(planning, result) == []
    assert [item.job_id for item in result.routes[0].jobs] == [1]
    assert [item.job_id for item in result.unassigned] == [2]


async def test_mandatory_job_without_qualification_rejects_the_model():
    planning = data([job(1, mandatory=True, required_qualifications=frozenset({9}))])
    with pytest.raises(SolverNoFeasibleSolution, match="no compatible engineer"):
        await OrToolsPlanningSolver(Provider()).solve(planning)


async def test_preallocated_equipment_stays_with_its_engineer():
    planning = data(
        [
            job(
                1,
                required_equipment=frozenset({1}),
                allowed_engineer_ids=frozenset({2}),
            )
        ],
        [engineer(1), engineer(2)],
        equipment_units={1: 1},
        preallocated_equipment_by_engineer={1: frozenset({1})},
    )
    result = await OrToolsPlanningSolver(Provider()).solve(planning)
    assert PlanningValidator().validate(planning, result) == []
    assert result.routes == []
    assert [item.job_id for item in result.unassigned] == [1]


async def test_empty_input_keeps_fixed_distance_and_pre_unassigned_without_io():
    planning = data(
        [],
        pre_unassigned=[UnassignedJob(42, 9000, ReasonCode.DATASET_LIMIT)],
        fixed_distance_legs_by_engineer={99: (11, 11)},
        fixed_active_engineer_ids=frozenset({99}),
    )
    provider = AsyncMock()
    result = await OrToolsPlanningSolver(provider).solve(planning)
    provider.get_matrix.assert_not_called()
    assert PlanningValidator().validate(planning, result) == []
    assert result.solver_status == "EMPTY"
    assert result.drop_cost == 9000
    assert result.objective == 4  # ceil(11/10) + ceil(11/10), без дорог к заявкам.
    assert result.objective_metrics["max_engineer_distance_meters"] == 22


async def test_travel_rounding_and_timezone_are_preserved():
    planning = data([job(1)], timezone="Asia/Yekaterinburg")
    provider = AsyncMock()
    provider.get_matrix.return_value = TravelMatrix(
        [[0, 61], [61, 0]], [[0, 11], [11, 0]], "auto", "TEST"
    )
    result = await OrToolsPlanningSolver(provider).solve(planning)
    assert PlanningValidator().validate(planning, result) == []
    visit = result.routes[0].jobs[0]
    assert visit.travel_from_previous_min == 2
    assert visit.planned_start.strftime("%H:%M") == "03:02"  # 08:02 UTC+5.
    assert visit.planned_start.utcoffset() == timedelta(0)
    assert result.travel_time_seconds_matrices["auto"][1][0] == 61
    assert visit.distance_from_previous_meters == 11


@pytest.mark.parametrize(
    ("seconds", "meters", "error_code"),
    [
        ([[0]], [[0]], "INVALID_TRAVEL_MATRIX_SHAPE"),
        ([[0, None], [60, 0]], [[0, 100], [100, 0]], "DISTANCE_DATA_NOT_READY"),
        ([[0, -1], [60, 0]], [[0, 100], [100, 0]], "DISTANCE_DATA_NOT_READY"),
        ([[0, 1.5], [60, 0]], [[0, 100], [100, 0]], "DISTANCE_DATA_NOT_READY"),
    ],
)
async def test_invalid_matrix_remains_an_error(seconds, meters, error_code):
    provider = AsyncMock()
    provider.get_matrix.return_value = TravelMatrix(seconds, meters, "auto", "TEST")
    with pytest.raises(RuntimeError, match=error_code):
        await OrToolsPlanningSolver(provider).solve(data([job(1)]))


async def test_provider_failure_is_not_reported_as_an_unassigned_job():
    provider = AsyncMock()
    provider.get_matrix.side_effect = ConnectionError("unavailable")
    with pytest.raises(RuntimeError, match="TRAVEL_PROVIDER_UNAVAILABLE"):
        await OrToolsPlanningSolver(provider).solve(data([job(1)]))


async def test_one_solver_can_run_independent_inputs_concurrently():
    import asyncio

    solver = OrToolsPlanningSolver(Provider())
    first = data([job(1)])
    second = data([replace(job(2), allowed_engineer_ids=frozenset({2}))], [engineer(2)])
    results = await asyncio.gather(solver.solve(first), solver.solve(second))
    for planning, result in zip((first, second), results, strict=True):
        assert PlanningValidator().validate(planning, result) == []
        assert result.routes[0].jobs[0].job_id == planning.jobs[0].id
        assert result.routes[0].engineer_id == planning.engineers[0].id
