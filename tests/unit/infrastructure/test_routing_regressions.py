"""Routing business invariants, including counterexamples found in the audit."""

import asyncio
import itertools
import random
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from time import monotonic
from unittest.mock import AsyncMock

import pytest

from planning.application.errors import SolverTimeLimit
from planning.application.interfaces.travel_matrix_provider import TravelMatrix
from planning.application.services.dynamic_today_planning import (
    DynamicTodayPlanningService,
    _candidate_score,
    _with_sla_hierarchy,
)
from planning.application.services.planning_input_normalizer import (
    PlanningInputNormalizer,
)
from planning.application.validators.planning_result import PlanningValidator
from planning.domain.entities.coordinate import Coordinate
from planning.domain.entities.engineer import Engineer
from planning.domain.entities.job import Job
from planning.domain.entities.planning import PlanningInput
from planning.domain.enums import TransportType
from planning.infrastructure.adapters.planning_solver_ortools import (
    OrToolsPlanningSolver,
)
from tests.unit.application.test_multiday_planning import config

DAY = date(2030, 1, 1)
NOW = datetime(2030, 1, 1, 7, tzinfo=timezone.utc)


def job(i, **kw):
    return replace(
        Job(
            i,
            DAY,
            30,
            Coordinate(0, i / 1000),
            480,
            1080,
            None,
            frozenset(),
            frozenset(),
            NOW,
            10000,
        ),
        **kw,
    )


def engineer(i=1, **kw):
    return replace(
        Engineer(i, TransportType.CAR, Coordinate(0, i), 480, 1080, frozenset()), **kw
    )


def data(jobs, engineers=None, **kw):
    return replace(
        PlanningInput(
            1,
            DAY,
            "UTC",
            config(),
            jobs,
            engineers or [engineer()],
            {},
            [],
            len(jobs),
            frozenset(j.id for j in jobs),
            {},
        ),
        **kw,
    )


class Provider:
    def __init__(self, minutes=None, distances=None):
        self.minutes = minutes
        self.distances = distances

    async def get_matrix(self, coordinates, profile, cache_ttl_days=None):
        minutes = self.minutes or [
            [0 if a == b else 1 for b in coordinates] for a in coordinates
        ]
        return TravelMatrix(
            [[None if x is None else x * 60 for x in row] for row in minutes],
            self.distances
            or [[None if x is None else x * 100 for x in row] for row in minutes],
            profile,
            "TEST",
        )


async def solve(d, minutes=None, distances=None):
    r = await OrToolsPlanningSolver(Provider(minutes, distances)).solve(d)
    assert PlanningValidator().validate(d, r) == []
    return r


async def test_emergency_outweighs_two_normal_jobs_in_same_sla_group():
    d = data(
        [
            job(1, priority_type="EMERGENCY", duration_min=100, drop_penalty=1009000),
            job(2, duration_min=50),
            job(3, duration_min=50),
        ],
        [engineer(shift_end_min=582)],
    )
    d = _with_sla_hierarchy(d, {j.id: j.drop_penalty for j in d.jobs}, DAY)
    r = await solve(d)
    assert [j.job_id for route in r.routes for j in route.jobs] == [1]


async def test_earlier_sla_group_outweighs_later_emergency():
    d = data(
        [
            job(1, sla_date=DAY - timedelta(days=1), duration_min=60),
            job(2, priority_type="EMERGENCY", duration_min=60, drop_penalty=1009000),
        ],
        [engineer(shift_end_min=541)],
    )
    d = _with_sla_hierarchy(d, {j.id: j.drop_penalty for j in d.jobs}, DAY)
    r = await solve(d)
    assert [j.job_id for route in r.routes for j in route.jobs] == [1]


async def test_guided_search_matches_exhaustive_optimum_on_six_job_regression():
    rng = random.Random(0)
    points = [(rng.randrange(30), rng.randrange(30)) for _ in range(7)]
    minutes = [[abs(a[0] - b[0]) + abs(a[1] - b[1]) for b in points] for a in points]
    jobs = [
        job(
            i + 1,
            duration_min=rng.choice([15, 30, 45]),
            window_start_min=480 + rng.choice([0, 30, 60, 120]),
            window_end_min=480 + rng.choice([180, 240, 300]),
            drop_penalty=rng.choice([1000, 2000, 4000]),
        )
        for i in range(6)
    ]
    d = data(jobs, [engineer(shift_end_min=780)])
    best = None
    for count in range(7):
        for order in itertools.permutations(range(6), count):
            minute, previous, travel = 480, 6, 0
            for i in order:
                minute = max(minute + minutes[previous][i], jobs[i].window_start_min)
                if (
                    minute > jobs[i].window_end_min
                    or minute + jobs[i].duration_min > 780
                ):
                    break
                minute += jobs[i].duration_min
                travel += minutes[previous][i]
                previous = i
            else:
                score = (
                    sum(j.drop_penalty for i, j in enumerate(jobs) if i not in order),
                    int(bool(order)),
                    travel * 10,
                    travel * 10,
                    travel,
                )
                best = score if best is None else min(best, score)
    r = await solve(d, minutes)
    actual = (
        r.drop_cost,
        len(r.routes),
        r.routes[0].total_distance_meters // 10,
        r.routes[0].total_distance_meters // 10,
        r.routes[0].total_travel_min,
    )
    assert actual == best
    assert sum(len(route.jobs) for route in r.routes) == 6


async def test_unreachable_reverse_arc_does_not_block_feasible_forward_route():
    d = data(
        [job(1, window_end_min=500), job(2, window_start_min=540, window_end_min=600)]
    )
    r = await solve(d, [[0, 10, 10], [None, 0, 10], [5, 10, 0]])
    assert [j.job_id for route in r.routes for j in route.jobs] == [1, 2]


async def test_coarse_time_units_remain_within_proven_bound():
    d = data(
        [job(1, duration_min=10), job(2, duration_min=10)],
        [engineer(shift_end_min=540)],
        config=config(time_unit_seconds=3600),
    )
    r = await solve(d)
    assert len(r.routes[0].jobs) == 2
    assert r.objective_metrics["tmax"] >= 2


async def test_final_service_must_finish_before_shift_end():
    d = data([job(1, duration_min=60)], [engineer(shift_end_min=540)])
    r = await solve(d)
    assert [j.job_id for j in r.unassigned] == [1]


async def test_equipment_is_allocated_per_engineer_not_per_job():
    d = data(
        [
            job(1, required_equipment=frozenset({1})),
            job(2, required_equipment=frozenset({1})),
        ],
        [engineer(1), engineer(2)],
        equipment_units={1: 1},
    )
    r = await solve(d)
    assert len(r.routes) == 1
    assert len(r.routes[0].jobs) == 2


@pytest.mark.parametrize(
    "fixed", [{1: (1000,)}, {3: (1000,)}, {1: (1000,), 3: (2000,)}]
)
async def test_full_day_maximum_includes_frozen_and_outside_routes(fixed):
    d = data(
        [job(1, mandatory=True)],
        [engineer(1), engineer(2)],
        fixed_active_engineer_ids=frozenset({1, 2}),
        fixed_distance_legs_by_engineer=fixed,
    )
    r = await solve(d, distances=[[0, 100, 100], [100, 0, 100], [100, 100, 0]])
    if fixed == {1: (1000,)}:
        assert r.routes[0].engineer_id == 2
    assert r.objective_metrics["max_engineer_distance_meters"] >= max(
        sum(x) for x in fixed.values()
    )


async def test_validator_rejects_early_departure_missing_matrix_and_second_route():
    d = data([job(1)])
    r = await solve(d)
    r.routes[0].planned_start -= timedelta(minutes=10)
    assert any(
        "outside engineer shift" in error
        for error in PlanningValidator().validate(d, r)
    )
    r.routes.append(r.routes[0])
    assert any(
        "multiple routes" in error for error in PlanningValidator().validate(d, r)
    )
    r.travel_matrices.clear()
    assert any("matrix" in error for error in PlanningValidator().validate(d, r))


async def test_event_snapshot_reuses_shared_pairs_and_caps_matrix_wait():
    class ChangingProvider(Provider):
        calls = 0

        async def get_matrix(self, *args):
            self.calls += 1
            r = await super().get_matrix(*args)
            return replace(
                r,
                distance_meters=[
                    [x * self.calls for x in row] for row in r.distance_meters
                ],
            )

    p = ChangingProvider()
    shared = {}
    d1 = data([job(1)], travel_snapshot=shared)
    r1 = await OrToolsPlanningSolver(p).solve(d1)
    d2 = data([job(1), job(2)], travel_snapshot=shared)
    r2 = await OrToolsPlanningSolver(p).solve(d2)
    assert r1.distance_matrices["auto"][1][0] == r2.distance_matrices["auto"][2][0]

    class SlowProvider(Provider):
        async def get_matrix(self, *args):
            await asyncio.sleep(1)

    with pytest.raises(SolverTimeLimit, match="MATRIX_TIME_LIMIT"):
        await OrToolsPlanningSolver(SlowProvider()).solve(
            data([job(1)], solve_deadline_monotonic=monotonic() + 0.02)
        )


async def test_infeasible_first_candidate_does_not_abort_second_engineer():
    raw = dict(
        id=1,
        sla_date=DAY.isoformat(),
        work_type_id=1,
        service_duration_min=60,
        default_service_duration_min=60,
        required_transport=None,
        time_window_start="08:00:00",
        time_window_end="08:20:00",
        latitude=0,
        longitude=0,
        address="job",
        created_at=NOW.isoformat(),
        status="NEW",
    )
    source = dict(
        project={"timezone": "UTC"},
        config=asdict(config(candidate_solver_time_limit_sec=1)),
        jobs=[raw],
        engineers=[
            dict(
                id=i,
                transport_type="CAR",
                start_address="base",
                start_latitude=0,
                start_longitude=i,
            )
            for i in (1, 2)
        ],
        schedules=[
            dict(
                engineer_id=i,
                work_date=DAY.isoformat(),
                shift_start="08:00:00",
                shift_end="10:00:00",
            )
            for i in (1, 2)
        ],
        engineer_qualifications={},
        required_qualifications={},
        required_equipment={},
        equipment_units={},
    )

    class CandidateProvider:
        async def get_matrix(self, coordinates, profile, cache_ttl_days=None):
            seconds = [
                [
                    0 if a == b else (1800 if a.longitude == 1 else 300)
                    for b in coordinates
                ]
                for a in coordinates
            ]
            return TravelMatrix(
                seconds, [[x * 10 for x in row] for row in seconds], profile, "TEST"
            )

    class Factory:
        def create(self, provider):
            return OrToolsPlanningSolver(CandidateProvider())

    repo = AsyncMock()
    service = DynamicTodayPlanningService(
        PlanningInputNormalizer(AsyncMock()), Factory(), PlanningValidator(), repo
    )
    context = dict(
        source=source, maximum_end=DAY + timedelta(days=29), snapshot_time=NOW
    )
    assignments, inserted = await service._insert_one(
        project_id=1,
        event_id=1,
        planning_date=DAY,
        context=context,
        assignments=[],
        new_job=raw,
        jobs_by_id={1: raw},
    )
    assert inserted
    assert assignments[0]["engineer_id"] == 2
    assert repo.save_candidate.await_count == 2
    assert repo.save_candidate.await_args_list[0].args[0]["solver_status"] == "REJECTED"
    assert context["today_solver_runs"][0][0].snapshot["snapshot_time"] == NOW


def test_candidate_ranking_preserves_emergency_before_normal_count():
    def assignment(i, priority):
        return dict(
            job_id=i,
            sla_date=DAY,
            priority_type=priority,
            engineer_id=1,
            sequence=i,
            status="NEW",
            planned_start=NOW + timedelta(hours=i),
        )

    before = [
        assignment(1, "EMERGENCY"),
        assignment(2, "NORMAL"),
        assignment(3, "NORMAL"),
    ]
    new = assignment(4, "EMERGENCY")
    common = ({1, 2, 3}, 4, NOW, DAY, {1: 1009000, 2: 9000, 3: 9000})
    keep_emergency = _candidate_score(before, [before[0], new], *common)
    keep_two_normals = _candidate_score(before, [before[1], before[2], new], *common)
    assert keep_emergency < keep_two_normals


async def test_urgent_insert_preserves_executing_job_and_next_stop():
    snapshot_time = NOW + timedelta(hours=1, minutes=10)
    raws = [
        dict(
            id=i,
            sla_date=DAY.isoformat(),
            work_type_id=1,
            service_duration_min=30,
            default_service_duration_min=30,
            required_transport=None,
            time_window_start="08:00:00",
            time_window_end="15:00:00",
            latitude=0,
            longitude=i / 1000,
            address=f"job {i}",
            created_at=NOW.isoformat(),
            status="IN_PROGRESS" if i == 1 else "NEW",
        )
        for i in range(1, 5)
    ]
    source = dict(
        project={"timezone": "UTC"},
        config=asdict(config(candidate_solver_time_limit_sec=1)),
        jobs=raws,
        engineers=[
            dict(
                id=1,
                transport_type="CAR",
                start_address="base",
                start_latitude=0,
                start_longitude=1,
            )
        ],
        schedules=[
            dict(
                engineer_id=1,
                work_date=DAY.isoformat(),
                shift_start="08:00:00",
                shift_end="16:00:00",
            )
        ],
        engineer_qualifications={},
        required_qualifications={},
        required_equipment={},
        equipment_units={},
    )
    assignments = []
    for i in range(1, 4):
        start = NOW + timedelta(hours=1, minutes=(i - 1) * 31 + 1)
        assignments.append(
            dict(
                job_id=i,
                planning_date=DAY,
                engineer_id=1,
                sequence=i,
                planned_arrival=start,
                planned_start=start,
                planned_finish=start + timedelta(minutes=30),
                travel_from_previous_min=1,
                distance_from_previous_meters=100,
                waiting_before_job_min=0,
                status=raws[i - 1]["status"],
                sla_date=DAY,
                work_type_id=1,
                address=f"job {i}",
                latitude=0,
                longitude=i / 1000,
            )
        )

    class Factory:
        def create(self, provider):
            return OrToolsPlanningSolver(Provider())

    context = dict(
        source=source, maximum_end=DAY + timedelta(days=29), snapshot_time=snapshot_time
    )
    service = DynamicTodayPlanningService(
        PlanningInputNormalizer(AsyncMock()),
        Factory(),
        PlanningValidator(),
        AsyncMock(),
    )
    result, inserted = await service._insert_one(
        project_id=1,
        event_id=1,
        planning_date=DAY,
        context=context,
        assignments=assignments,
        new_job=raws[3],
        jobs_by_id={x["id"]: x for x in raws},
    )
    assert inserted
    for old, new in zip(assignments[:2], result[:2], strict=True):
        for field in (
            "job_id",
            "engineer_id",
            "sequence",
            "planned_start",
            "planned_finish",
        ):
            assert new[field] == old[field]
    assert {x["job_id"] for x in result} == {1, 2, 3, 4}
    assert all(x["engineer_id"] == 1 for x in result)
    candidate_data, candidate_result = context["today_solver_runs"][0]
    assert candidate_data.fixed_distance_legs_by_engineer == {1: (100, 100)}
    assert candidate_result.objective_metrics["max_engineer_distance_meters"] == 400


async def test_dataset_limit_cannot_displace_overdue_jobs_with_later_emergency():
    rows = [
        dict(
            id=i,
            sla_date=(DAY - timedelta(days=1) if i < 3 else DAY).isoformat(),
            priority_type="EMERGENCY" if i == 3 else "NORMAL",
            work_type_id=1,
            service_duration_min=30,
            default_service_duration_min=30,
            required_transport=None,
            time_window_start="08:00:00",
            time_window_end="15:00:00",
            latitude=0,
            longitude=i / 1000,
            address=f"job {i}",
            created_at=NOW.isoformat(),
            status="NEW",
        )
        for i in range(1, 4)
    ]
    source = dict(
        project={"timezone": "UTC"},
        config=asdict(config(max_jobs_per_run=2)),
        jobs=rows,
        engineers=[
            dict(
                id=1,
                engineer_id=1,
                transport_type="CAR",
                start_address="base",
                start_latitude=0,
                start_longitude=1,
                shift_start="08:00:00",
                shift_end="16:00:00",
            )
        ],
        engineer_qualifications={},
        required_qualifications={},
        required_equipment={},
        equipment_units={},
    )
    from datetime import time

    for row in rows:
        row["sla_date"] = date.fromisoformat(row["sla_date"])
        row["created_at"] = NOW
        row["time_window_start"] = time(8)
        row["time_window_end"] = time(15)
    source["engineers"][0].update(shift_start=time(8), shift_end=time(16))
    normalized = await PlanningInputNormalizer(AsyncMock()).normalize(
        1, DAY, "UTC", source, snapshot_time=NOW
    )
    import json

    from planning.infrastructure.adapters.planning_run_repository_sqla import _jsonable

    saved = json.loads(json.dumps(_jsonable(normalized.snapshot)))
    assert saved["jobs"][0]["time_window_start"] == "08:00:00"
    assert saved["jobs"][0]["address"] == "job 1"
    assert {j.id for j in normalized.jobs} == {1, 2}
    assert [j.job_id for j in normalized.pre_unassigned] == [3]
