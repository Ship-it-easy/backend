from dataclasses import replace
from datetime import datetime, timedelta, timezone
from time import perf_counter

import pytest

from planning.application.services.baseline_fifo import (
    BaselineCalculationError,
    _hash,
    baseline_applicable,
    calculate_fifo_baseline,
    compare_with_optimized,
    not_applicable_result,
    required_baseline_travel_arcs,
    validate_fifo_baseline,
)
from planning.domain.entities.planning import PlanningResult, Route, RouteJob
from planning.domain.enums import TransportType
from planning.infrastructure.adapters.ortools_routing.travel import (
    prepare_travel_matrices_for_jobs,
)
from tests.unit.infrastructure.test_routing_regressions import (
    data,
    engineer,
    job,
)


def matrix(size: int, default: int = 100) -> list[list[int | None]]:
    return [
        [0 if left == right else default for left in range(size)]
        for right in range(size)
    ]


def baseline_data(jobs, engineers):
    return data(
        jobs,
        engineers,
        baseline_jobs=jobs,
        snapshot={
            "snapshot_time": datetime(2030, 1, 1, 7, tzinfo=timezone.utc),
            "baseline_earliest_shift_start_min": 480,
        },
    )


def test_hash_normalizes_unordered_snapshot_sets():
    assert _hash({"qualification_ids": frozenset({3, 1, 2})}) == _hash(
        {"qualification_ids": [1, 2, 3]}
    )


def test_current_day_after_first_shift_is_not_applicable():
    planning = baseline_data([job(1)], [engineer()])
    planning = replace(
        planning,
        snapshot={
            **planning.snapshot,
            "snapshot_time": datetime(2030, 1, 1, 8, 5, tzinfo=timezone.utc),
        },
    )
    applicable, earliest = baseline_applicable(planning)
    assert applicable is False
    assert earliest == datetime(2030, 1, 1, 8, tzinfo=timezone.utc)
    assert not_applicable_result(planning).status == "NOT_APPLICABLE_SHIFT_STARTED"


def test_future_day_is_applicable_even_after_today_shift():
    planning = baseline_data([job(1)], [engineer()])
    planning = replace(
        planning,
        snapshot={
            **planning.snapshot,
            "snapshot_time": datetime(2029, 12, 31, 10, tzinfo=timezone.utc),
        },
    )
    assert baseline_applicable(planning)[0] is True


@pytest.mark.asyncio
async def test_fifo_uses_only_required_cached_route_arcs():
    planning = baseline_data([job(1), job(2)], [engineer()])
    required = required_baseline_travel_arcs(planning)
    assert len(required) == 2
    planning.travel_snapshot.update({key: (60, 123) for key in required})

    class UnavailableProvider:
        async def get_matrix(self, *args, **kwargs):
            raise AssertionError("no external request is needed")

    travel = await prepare_travel_matrices_for_jobs(
        planning,
        planning.baseline_jobs,
        UnavailableProvider(),
        required_arcs=required,
    )
    result = calculate_fifo_baseline(planning, travel.meters)
    assert result.assigned_jobs_count == 2
    assert result.total_distance_meters == 246
    assert sum(value is not None for row in travel.meters["auto"] for value in row) == 2


@pytest.mark.asyncio
async def test_fifo_fetches_when_required_arc_is_not_cached():
    planning = baseline_data([job(1)], [engineer()])
    required = required_baseline_travel_arcs(planning)

    class UnavailableProvider:
        async def get_matrix(self, *args, **kwargs):
            raise RuntimeError("road service unavailable")

    with pytest.raises(RuntimeError, match="TRAVEL_PROVIDER_UNAVAILABLE"):
        await prepare_travel_matrices_for_jobs(
            planning,
            planning.baseline_jobs,
            UnavailableProvider(),
            required_arcs=required,
        )


def test_fifo_100_jobs_20_engineers_p95_under_two_seconds():
    planning = baseline_data(
        [job(index) for index in range(1, 101)],
        [engineer(index) for index in range(1, 21)],
    )
    distances = {"auto": matrix(120)}
    durations = []
    for _ in range(20):
        started = perf_counter()
        result = calculate_fifo_baseline(planning, distances)
        durations.append(perf_counter() - started)
        assert result.assigned_jobs_count == 100
    assert sorted(durations)[18] < 2


def test_fifo_order_capacity_and_engineer_scan_are_deterministic():
    received = datetime(2030, 1, 1, 6, tzinfo=timezone.utc)
    jobs = [
        job(30, received_at=received + timedelta(minutes=1), ingest_sequence=1),
        job(20, received_at=received, ingest_sequence=2),
        job(10, received_at=received, ingest_sequence=1),
    ]
    engineers = [
        engineer(
            2,
            shift_end_min=580,
            created_at=datetime(2029, 1, 2, tzinfo=timezone.utc),
        ),
        engineer(
            1,
            shift_end_min=580,
            created_at=datetime(2029, 1, 1, tzinfo=timezone.utc),
        ),
    ]
    result = calculate_fifo_baseline(
        baseline_data(jobs, engineers), {"auto": matrix(5)}
    )

    assert result.status == "READY"
    assert [[item.job_id for item in route.jobs] for route in result.routes] == [
        [10, 20],
        [30],
    ]
    assert [route.engineer_id for route in result.routes] == [1, 2]
    assert result.assigned_jobs_count == 3
    assert result.unassigned_jobs_count == 0
    assert result.result_hash

    repeated = calculate_fifo_baseline(
        baseline_data(jobs, engineers), {"auto": matrix(5)}
    )
    assert repeated.input_hash == result.input_hash
    assert repeated.travel_matrix_hash == result.travel_matrix_hash
    assert repeated.result_hash == result.result_hash


def test_fifo_validator_rejects_inconsistent_aggregate():
    planning = baseline_data([job(1)], [engineer()])
    distances = {"auto": matrix(2, 100)}
    result = calculate_fifo_baseline(planning, distances)

    with pytest.raises(BaselineCalculationError) as error:
        validate_fifo_baseline(
            planning,
            replace(result, total_distance_meters=result.total_distance_meters + 1),
            distances,
        )

    assert error.value.code == "BASELINE_VALIDATION_FAILED"


def test_fifo_ignores_equipment_and_window_for_assignment_and_accepts_exact_fit():
    jobs = [
        job(
            1,
            duration_min=40,
            window_start_min=700,
            window_end_min=710,
            required_equipment=frozenset({999}),
        )
    ]
    result = calculate_fifo_baseline(
        baseline_data(jobs, [engineer(shift_start_min=480, shift_end_min=540)]),
        {"auto": matrix(2, 250)},
    )

    visit = result.routes[0].jobs[0]
    assert visit.planned_start.hour == 8 and visit.planned_start.minute == 20
    assert visit.planned_finish.hour == 9 and visit.planned_finish.minute == 0
    assert visit.window_hit is False
    assert result.routes[0].remaining_minutes == 0
    assert result.window_hit_count == 0
    assert result.window_hit_rate == 0


def test_fifo_uses_qualification_transport_and_capacity_reasons():
    jobs = [
        job(1, required_qualifications=frozenset({9})),
        job(2, required_transport=TransportType.CAR),
        job(3, duration_min=100),
    ]
    result = calculate_fifo_baseline(
        baseline_data(
            jobs,
            [
                engineer(
                    transport_type=TransportType.NONE,
                    shift_start_min=480,
                    shift_end_min=540,
                )
            ],
        ),
        {"pedestrian": matrix(4)},
    )

    assert [item.reason_code for item in result.unassigned] == [
        "BASELINE_NO_QUALIFICATION",
        "BASELINE_NO_TRANSPORT",
        "BASELINE_SHIFT_CAPACITY_EXCEEDED",
    ]


def test_fifo_requires_all_qualifications_and_uses_engineer_travel_profile():
    jobs = [
        job(1, required_qualifications=frozenset({1, 2})),
        job(2),
    ]
    engineers = [
        engineer(
            1,
            transport_type=TransportType.NONE,
            qualifications=frozenset({1}),
            shift_start_min=480,
            shift_end_min=530,
        ),
        engineer(
            2,
            qualifications=frozenset({1, 2}),
            shift_start_min=480,
            shift_end_min=530,
        ),
    ]
    pedestrian = matrix(4, 10)
    auto = matrix(4, 20)
    # Job 1 skips the partially qualified pedestrian engineer and uses auto.
    auto[3][0] = 222
    # Job 2 restarts from the pedestrian engineer, which still has capacity.
    pedestrian[2][1] = 111

    result = calculate_fifo_baseline(
        baseline_data(jobs, engineers),
        {"pedestrian": pedestrian, "auto": auto},
    )

    assert [route.engineer_id for route in result.routes] == [1, 2]
    assert [item.job_id for item in result.routes[0].jobs] == [2]
    assert [item.job_id for item in result.routes[1].jobs] == [1]
    assert [route.distance_meters for route in result.routes] == [111, 222]
    assert result.total_distance_meters == 333
    assert result.active_engineer_count == 2


def test_fifo_restarts_engineer_scan_for_every_job():
    jobs = [
        job(1, duration_min=30),
        job(2, duration_min=60),
        job(3, duration_min=20),
    ]
    engineers = [
        engineer(1, shift_start_min=480, shift_end_min=580),
        engineer(2, shift_start_min=480, shift_end_min=660),
    ]

    result = calculate_fifo_baseline(
        baseline_data(jobs, engineers), {"auto": matrix(5)}
    )

    assert [item.job_id for item in result.routes[0].jobs] == [1, 3]
    assert [item.job_id for item in result.routes[1].jobs] == [2]


def test_fifo_rejects_job_that_exceeds_remaining_shift_by_one_minute():
    result = calculate_fifo_baseline(
        baseline_data(
            [job(1, duration_min=41)],
            [engineer(shift_start_min=480, shift_end_min=540)],
        ),
        {"auto": matrix(2)},
    )

    assert result.assigned_jobs_count == 0
    assert result.unassigned[0].reason_code == "BASELINE_SHIFT_CAPACITY_EXCEEDED"


def test_fifo_window_boundaries_are_inclusive_and_empty_rate_is_null():
    jobs = [
        job(1, window_start_min=500, window_end_min=500),
        job(2, window_start_min=550, window_end_min=550),
    ]
    result = calculate_fifo_baseline(
        baseline_data(jobs, [engineer()]), {"auto": matrix(3)}
    )

    assert [item.window_hit for item in result.routes[0].jobs] == [True, True]
    assert result.window_hit_rate == 100

    no_engineers = replace(baseline_data(jobs, [engineer()]), engineers=[])
    empty = calculate_fifo_baseline(no_engineers, {})
    assert empty.assigned_jobs_count == 0
    assert empty.unassigned_jobs_count == 2
    assert empty.window_hit_rate is None
    assert {item.reason_code for item in empty.unassigned} == {
        "BASELINE_NO_VALID_SHIFT"
    }


def test_fifo_counts_window_misses_before_and_after_the_window():
    jobs = [
        job(1, window_start_min=600, window_end_min=700),
        job(2, window_start_min=480, window_end_min=549),
    ]

    result = calculate_fifo_baseline(
        baseline_data(jobs, [engineer()]), {"auto": matrix(3)}
    )

    assert [item.planned_start.minute for item in result.routes[0].jobs] == [20, 10]
    assert [item.window_hit for item in result.routes[0].jobs] == [False, False]
    assert result.window_miss_count == 2
    assert result.window_hit_rate == 0


@pytest.mark.parametrize(
    ("planning", "code"),
    [
        (
            baseline_data([job(1, duration_min=0)], [engineer()]),
            "BASELINE_INVALID_DURATION",
        ),
        (
            baseline_data(
                [job(1)],
                [engineer(shift_start_min=600, shift_end_min=600)],
            ),
            "BASELINE_INVALID_SHIFT",
        ),
    ],
)
def test_fifo_rejects_invalid_validated_input(planning, code):
    with pytest.raises(BaselineCalculationError) as error:
        calculate_fifo_baseline(planning, {"auto": matrix(2)})
    assert error.value.code == code


def test_fifo_distance_uses_start_and_between_jobs_without_return():
    jobs = [job(1), job(2)]
    meters = matrix(3, 0)
    # Matrix order is jobs first, then engineer start.
    meters[2][0] = 700
    meters[0][1] = 300
    meters[1][2] = 9000  # Return leg must not be counted.
    result = calculate_fifo_baseline(
        baseline_data(jobs, [engineer()]), {"auto": meters}
    )

    assert result.total_distance_meters == 1000
    assert [item.distance_from_previous_meters for item in result.routes[0].jobs] == [
        700,
        300,
    ]


def test_fifo_fails_when_a_required_distance_arc_is_missing():
    meters = matrix(2)
    meters[1][0] = None
    with pytest.raises(BaselineCalculationError, match="Distance is missing") as error:
        calculate_fifo_baseline(baseline_data([job(1)], [engineer()]), {"auto": meters})
    assert error.value.code == "BASELINE_DISTANCE_DATA_NOT_READY"


def test_comparison_suppresses_winner_when_assigned_job_sets_differ():
    jobs = [job(1), job(2)]
    planning = baseline_data(jobs, [engineer(shift_end_min=530)])
    baseline = calculate_fifo_baseline(planning, {"auto": matrix(3, 100)})
    start = datetime(2030, 1, 1, 8, 30, tzinfo=timezone.utc)
    optimized = PlanningResult(
        routes=[
            Route(
                engineer_id=1,
                planned_start=start,
                planned_finish=start + timedelta(minutes=30),
                total_travel_min=1,
                total_service_min=30,
                total_waiting_min=0,
                jobs=[
                    RouteJob(
                        job_id=2,
                        sequence=1,
                        planned_arrival=start,
                        planned_start=start,
                        planned_finish=start + timedelta(minutes=30),
                        travel_from_previous_min=1,
                        waiting_before_job_min=0,
                        drop_penalty=1,
                        distance_from_previous_meters=50,
                    )
                ],
                equipment_type_ids=set(),
                total_distance_meters=50,
            )
        ],
        unassigned=[],
        solver_status="OPTIMAL",
        objective=0,
        drop_cost=0,
        travel_cost=0,
        solver_time_ms=1,
    )

    comparison = compare_with_optimized(planning, optimized, baseline)

    assert comparison is not None
    assert comparison.coverage_comparable is False
    assert comparison.baseline_metrics["assigned_jobs_count"] == 1
    assert comparison.optimized_metrics["assigned_jobs_count"] == 1
    assert comparison.deltas["total_distance_meters"] == -50
    assert comparison.optimized_metrics["unassigned_jobs_count"] == 1


def test_comparison_includes_engineers_used_only_by_one_plan():
    planning = baseline_data([job(1)], [engineer(1), engineer(2)])
    baseline = calculate_fifo_baseline(planning, {"auto": matrix(3)})
    start = datetime(2030, 1, 1, 8, 30, tzinfo=timezone.utc)
    optimized = PlanningResult(
        routes=[
            Route(
                engineer_id=2,
                planned_start=start,
                planned_finish=start + timedelta(minutes=30),
                total_travel_min=1,
                total_service_min=30,
                total_waiting_min=0,
                jobs=[
                    RouteJob(
                        job_id=1,
                        sequence=1,
                        planned_arrival=start,
                        planned_start=start,
                        planned_finish=start + timedelta(minutes=30),
                        travel_from_previous_min=1,
                        waiting_before_job_min=0,
                        drop_penalty=1,
                        distance_from_previous_meters=50,
                    )
                ],
                equipment_type_ids=set(),
                total_distance_meters=50,
            )
        ],
        unassigned=[],
        solver_status="OPTIMAL",
        objective=0,
        drop_cost=0,
        travel_cost=0,
        solver_time_ms=1,
    )

    comparison = compare_with_optimized(planning, optimized, baseline)

    assert comparison is not None
    assert comparison.coverage_comparable is True
    assert [item["engineer_id"] for item in comparison.engineer_metrics] == [2, 1]
    assert comparison.engineer_metrics[0]["baseline_jobs_count"] == 0
    assert comparison.engineer_metrics[0]["baseline_distance_meters"] == 0
    assert comparison.engineer_metrics[1]["optimized_jobs_count"] == 0
    assert comparison.engineer_metrics[1]["optimized_distance_meters"] == 0
