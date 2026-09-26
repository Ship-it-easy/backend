from __future__ import annotations

import hashlib
import json
import time as monotonic_time
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from planning.domain.entities.baseline import (
    BaselineComparison,
    BaselinePlanResult,
    BaselineRoute,
    BaselineRouteJob,
    BaselineUnassignedJob,
)
from planning.domain.entities.engineer import Engineer
from planning.domain.entities.job import Job
from planning.domain.entities.planning import PlanningInput, PlanningResult
from planning.domain.enums import TransportType

BASELINE_ALGORITHM_VERSION = "FIFO_V2"
BASELINE_TRAVEL_MINUTES = 20


class BaselineCalculationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def baseline_applicable(data: PlanningInput) -> tuple[bool, datetime | None]:
    snapshot_time = _datetime(data.snapshot.get("snapshot_time"))
    earliest_minute = data.snapshot.get("baseline_earliest_shift_start_min")
    if earliest_minute is None:
        earliest_minute = min(
            (engineer.shift_start_min for engineer in data.engineers),
            default=None,
        )
    earliest = (
        _utc_at(data.planning_date, int(earliest_minute), data.timezone)
        if earliest_minute is not None
        else None
    )
    if snapshot_time is None or earliest is None:
        return True, earliest
    local_snapshot = snapshot_time.astimezone(ZoneInfo(data.timezone))
    if local_snapshot.date() != data.planning_date:
        return True, earliest
    return local_snapshot < earliest, earliest


def not_applicable_result(data: PlanningInput) -> BaselinePlanResult:
    _, earliest = baseline_applicable(data)
    input_count = len(data.baseline_jobs or data.jobs)
    return BaselinePlanResult(
        project_id=data.project_id,
        planning_date=data.planning_date,
        status="NOT_APPLICABLE_SHIFT_STARTED",
        algorithm_version=BASELINE_ALGORITHM_VERSION,
        input_hash=_input_hash(data),
        travel_matrix_hash=None,
        input_jobs_count=input_count,
        assigned_jobs_count=0,
        unassigned_jobs_count=0,
        window_hit_count=0,
        window_miss_count=0,
        window_hit_rate=None,
        active_engineer_count=0,
        total_distance_meters=0,
        earliest_shift_start=earliest,
    )


def pending_result(data: PlanningInput) -> BaselinePlanResult:
    _, earliest = baseline_applicable(data)
    return BaselinePlanResult(
        project_id=data.project_id,
        planning_date=data.planning_date,
        status="PENDING",
        algorithm_version=BASELINE_ALGORITHM_VERSION,
        input_hash=_input_hash(data),
        travel_matrix_hash=None,
        input_jobs_count=len(data.baseline_jobs or data.jobs),
        assigned_jobs_count=0,
        unassigned_jobs_count=0,
        window_hit_count=0,
        window_miss_count=0,
        window_hit_rate=None,
        active_engineer_count=0,
        total_distance_meters=0,
        earliest_shift_start=earliest,
    )


def failed_result(
    data: PlanningInput,
    code: str,
    message: str,
    *,
    started_at: float | None = None,
) -> BaselinePlanResult:
    _, earliest = baseline_applicable(data)
    input_count = len(data.baseline_jobs or data.jobs)
    elapsed = (
        int((monotonic_time.perf_counter() - started_at) * 1000)
        if started_at is not None
        else 0
    )
    return BaselinePlanResult(
        project_id=data.project_id,
        planning_date=data.planning_date,
        status="FAILED",
        algorithm_version=BASELINE_ALGORITHM_VERSION,
        input_hash=_input_hash(data),
        travel_matrix_hash=None,
        input_jobs_count=input_count,
        assigned_jobs_count=0,
        unassigned_jobs_count=0,
        window_hit_count=0,
        window_miss_count=0,
        window_hit_rate=None,
        active_engineer_count=0,
        total_distance_meters=0,
        calculation_time_ms=elapsed,
        earliest_shift_start=earliest,
        failure_code=code,
        failure_message=message[:1000],
    )


def _assign_fifo_jobs(
    data: PlanningInput,
) -> tuple[
    list[Job],
    list[Engineer],
    dict[int, list[int]],
    dict[int, int],
    list[BaselineUnassignedJob],
]:
    """Assign without road data; only route arcs are needed for the audit."""
    ordered_jobs = sorted(data.baseline_jobs or data.jobs, key=_job_order)
    ordered_engineers = sorted(data.engineers, key=_engineer_order)
    route_job_ids = {engineer.id: [] for engineer in ordered_engineers}
    consumed = {engineer.id: 0 for engineer in ordered_engineers}
    unassigned: list[BaselineUnassignedJob] = []
    for input_order, job in enumerate(ordered_jobs, start=1):
        selected: Engineer | None = None
        qualified = 0
        transported = 0
        for engineer in ordered_engineers:
            if not job.required_qualifications.issubset(engineer.qualifications):
                continue
            qualified += 1
            if not _transport_matches(job, engineer):
                continue
            transported += 1
            required = job.duration_min + BASELINE_TRAVEL_MINUTES
            capacity = engineer.shift_end_min - engineer.shift_start_min
            if capacity - consumed[engineer.id] < required:
                continue
            selected = engineer
            break
        if selected is not None:
            route_job_ids[selected.id].append(job.id)
            consumed[selected.id] += job.duration_min + BASELINE_TRAVEL_MINUTES
            continue
        if not ordered_engineers:
            reason = "BASELINE_NO_VALID_SHIFT"
        elif qualified == 0:
            reason = "BASELINE_NO_QUALIFICATION"
        elif transported == 0:
            reason = "BASELINE_NO_TRANSPORT"
        else:
            reason = "BASELINE_SHIFT_CAPACITY_EXCEEDED"
        unassigned.append(
            BaselineUnassignedJob(
                job_id=job.id,
                job_input_order=input_order,
                reason_code=reason,
                diagnostics={
                    "engineers_checked": len(ordered_engineers),
                    "qualified_engineers": qualified,
                    "transport_compatible_engineers": transported,
                },
            )
        )
    return ordered_jobs, ordered_engineers, route_job_ids, consumed, unassigned


def required_baseline_travel_arcs(
    data: PlanningInput,
) -> set[tuple[str, float, float, float, float]]:
    """Only arcs traversed by the immutable FIFO assignment need distances."""
    _, engineers, route_job_ids, _, _ = _assign_fifo_jobs(data)
    jobs_by_id = {job.id: job for job in (data.baseline_jobs or data.jobs)}
    required: set[tuple[str, float, float, float, float]] = set()
    for engineer in engineers:
        profile = (
            "auto" if engineer.transport_type == TransportType.CAR else "pedestrian"
        )
        previous = engineer.coordinate
        for job_id in route_job_ids[engineer.id]:
            destination = jobs_by_id[job_id].coordinate
            required.add(
                (
                    profile,
                    previous.latitude,
                    previous.longitude,
                    destination.latitude,
                    destination.longitude,
                )
            )
            previous = destination
    return required


def calculate_fifo_baseline(
    data: PlanningInput,
    distance_matrices: dict[str, list[list[int | None]]],
) -> BaselinePlanResult:
    started = monotonic_time.perf_counter()
    if any(job.duration_min <= 0 for job in (data.baseline_jobs or data.jobs)):
        raise BaselineCalculationError(
            "BASELINE_INVALID_DURATION",
            "Baseline input contains a non-positive service duration",
        )
    if any(
        engineer.shift_start_min >= engineer.shift_end_min
        for engineer in data.engineers
    ):
        raise BaselineCalculationError(
            "BASELINE_INVALID_SHIFT",
            "Baseline input contains an invalid engineer shift",
        )
    applicable, earliest = baseline_applicable(data)
    if not applicable:
        return not_applicable_result(data)

    jobs = list(data.baseline_jobs or data.jobs)
    input_count = len(jobs)
    jobs_by_id = {job.id: job for job in jobs}
    matrix_job_index = {job.id: index for index, job in enumerate(jobs)}
    matrix_engineer_index = {
        engineer.id: len(jobs) + index for index, engineer in enumerate(data.engineers)
    }
    ordered_jobs, ordered_engineers, route_job_ids, consumed, unassigned = (
        _assign_fifo_jobs(data)
    )
    input_order = {job.id: index for index, job in enumerate(ordered_jobs, start=1)}
    engineer_order = {
        engineer.id: index for index, engineer in enumerate(ordered_engineers, start=1)
    }
    routes: list[BaselineRoute] = []
    for engineer in ordered_engineers:
        job_ids = route_job_ids[engineer.id]
        if not job_ids:
            continue
        profile = (
            "auto" if engineer.transport_type == TransportType.CAR else "pedestrian"
        )
        matrix = distance_matrices.get(profile)
        expected_size = len(jobs) + len(data.engineers)
        if (
            matrix is None
            or len(matrix) != expected_size
            or any(len(row) != expected_size for row in matrix)
        ):
            raise BaselineCalculationError(
                "BASELINE_DISTANCE_DATA_NOT_READY",
                f"Distance matrix for profile {profile} is incomplete",
            )
        current_minute = engineer.shift_start_min
        previous_index = matrix_engineer_index[engineer.id]
        assignments: list[BaselineRouteJob] = []
        route_distance = 0
        for position, job_id in enumerate(job_ids, start=1):
            job = jobs_by_id[job_id]
            current_minute += BASELINE_TRAVEL_MINUTES
            planned_start = _utc_at(data.planning_date, current_minute, data.timezone)
            planned_finish = planned_start + timedelta(minutes=job.duration_min)
            job_index = matrix_job_index[job.id]
            distance = matrix[previous_index][job_index]
            if distance is None or not isinstance(distance, int) or distance < 0:
                raise BaselineCalculationError(
                    "BASELINE_DISTANCE_DATA_NOT_READY",
                    "Distance is missing for baseline route arc ending at "
                    f"job {job.id}",
                )
            route_distance += distance
            assignments.append(
                BaselineRouteJob(
                    job_id=job.id,
                    job_input_order=input_order[job.id],
                    route_position=position,
                    received_at=job.received_at or job.created_at,
                    ingest_sequence=job.ingest_sequence,
                    service_duration_minutes=job.duration_min,
                    planned_start=planned_start,
                    planned_finish=planned_finish,
                    window_from_min=job.window_start_min,
                    window_to_min=job.window_end_min,
                    window_hit=(
                        job.window_start_min <= current_minute <= job.window_end_min
                    ),
                    previous_location_type=(
                        "ENGINEER_START" if position == 1 else "JOB"
                    ),
                    distance_from_previous_meters=distance,
                )
            )
            current_minute += job.duration_min
            previous_index = job_index
        capacity = engineer.shift_end_min - engineer.shift_start_min
        routes.append(
            BaselineRoute(
                engineer_id=engineer.id,
                engineer_name=engineer.name,
                engineer_input_order=engineer_order[engineer.id],
                shift_start=_utc_at(
                    data.planning_date, engineer.shift_start_min, data.timezone
                ),
                shift_end=_utc_at(
                    data.planning_date, engineer.shift_end_min, data.timezone
                ),
                capacity_minutes=capacity,
                consumed_minutes=consumed[engineer.id],
                remaining_minutes=capacity - consumed[engineer.id],
                distance_meters=route_distance,
                jobs=tuple(assignments),
                start_location_snapshot={
                    "latitude": engineer.coordinate.latitude,
                    "longitude": engineer.coordinate.longitude,
                },
            )
        )

    assigned_jobs = [item for route in routes for item in route.jobs]
    window_hits = sum(item.window_hit for item in assigned_jobs)
    assigned_count = len(assigned_jobs)
    matrix_hash = _hash(distance_matrices)
    provisional = BaselinePlanResult(
        project_id=data.project_id,
        planning_date=data.planning_date,
        status="READY",
        algorithm_version=BASELINE_ALGORITHM_VERSION,
        input_hash=_input_hash(data),
        travel_matrix_hash=matrix_hash,
        input_jobs_count=input_count,
        assigned_jobs_count=assigned_count,
        unassigned_jobs_count=len(unassigned),
        window_hit_count=window_hits,
        window_miss_count=assigned_count - window_hits,
        window_hit_rate=(
            window_hits / assigned_count * 100 if assigned_count else None
        ),
        active_engineer_count=len(routes),
        total_distance_meters=sum(route.distance_meters for route in routes),
        routes=tuple(routes),
        unassigned=tuple(unassigned),
        calculation_time_ms=int((monotonic_time.perf_counter() - started) * 1000),
        earliest_shift_start=earliest,
    )
    validate_fifo_baseline(data, provisional, distance_matrices)
    return BaselinePlanResult(
        **{
            **asdict(provisional),
            "routes": provisional.routes,
            "unassigned": provisional.unassigned,
            "result_hash": _hash(_baseline_hash_payload(provisional)),
        }
    )


def validate_fifo_baseline(
    data: PlanningInput,
    result: BaselinePlanResult,
    distance_matrices: dict[str, list[list[int | None]]],
) -> None:
    """Independently replay and verify FIFO invariants before persistence."""
    if result.status != "READY":
        _validation_failed("validator requires a READY baseline")
    jobs = list(data.baseline_jobs or data.jobs)
    engineers = list(data.engineers)
    jobs_by_id = {job.id: job for job in jobs}
    engineers_by_id = {engineer.id: engineer for engineer in engineers}
    if len(jobs_by_id) != len(jobs):
        _validation_failed("input contains duplicate job identifiers")
    if len(engineers_by_id) != len(engineers):
        _validation_failed("input contains duplicate engineer identifiers")

    ordered_jobs = sorted(jobs, key=_job_order)
    ordered_engineers = sorted(engineers, key=_engineer_order)
    job_order = {job.id: index for index, job in enumerate(ordered_jobs, start=1)}
    engineer_order = {
        engineer.id: index for index, engineer in enumerate(ordered_engineers, start=1)
    }
    route_by_engineer: dict[int, BaselineRoute] = {}
    assigned: dict[int, tuple[int, BaselineRouteJob]] = {}
    for route in result.routes:
        if route.engineer_id not in engineers_by_id:
            _validation_failed(f"unknown engineer {route.engineer_id} in route")
        if route.engineer_id in route_by_engineer:
            _validation_failed(f"duplicate route for engineer {route.engineer_id}")
        if not route.jobs:
            _validation_failed(f"empty route for engineer {route.engineer_id}")
        route_by_engineer[route.engineer_id] = route
        for item in route.jobs:
            if item.job_id in assigned:
                _validation_failed(f"job {item.job_id} is assigned more than once")
            assigned[item.job_id] = (route.engineer_id, item)

    unassigned: dict[int, BaselineUnassignedJob] = {}
    for item in result.unassigned:
        if item.job_id in unassigned:
            _validation_failed(f"job {item.job_id} is unassigned more than once")
        if item.job_id in assigned:
            _validation_failed(f"job {item.job_id} is both assigned and unassigned")
        unassigned[item.job_id] = item
    if set(assigned) | set(unassigned) != set(jobs_by_id):
        _validation_failed("assigned and unassigned rows do not cover the input jobs")

    expected_by_engineer: dict[int, list[int]] = {
        engineer.id: [] for engineer in ordered_engineers
    }
    expected_consumed = {engineer.id: 0 for engineer in ordered_engineers}
    expected_unassigned: dict[int, str] = {}
    for job in ordered_jobs:
        selected: Engineer | None = None
        qualified = 0
        transported = 0
        for engineer in ordered_engineers:
            if not job.required_qualifications.issubset(engineer.qualifications):
                continue
            qualified += 1
            if not _transport_matches(job, engineer):
                continue
            transported += 1
            required = job.duration_min + BASELINE_TRAVEL_MINUTES
            capacity = engineer.shift_end_min - engineer.shift_start_min
            if capacity - expected_consumed[engineer.id] < required:
                continue
            selected = engineer
            break
        if selected is not None:
            expected_by_engineer[selected.id].append(job.id)
            expected_consumed[selected.id] += (
                job.duration_min + BASELINE_TRAVEL_MINUTES
            )
            actual = assigned.get(job.id)
            if actual is None or actual[0] != selected.id:
                _validation_failed(
                    f"job {job.id} was not assigned to the first compatible engineer"
                )
            continue
        if not ordered_engineers:
            reason = "BASELINE_NO_VALID_SHIFT"
        elif qualified == 0:
            reason = "BASELINE_NO_QUALIFICATION"
        elif transported == 0:
            reason = "BASELINE_NO_TRANSPORT"
        else:
            reason = "BASELINE_SHIFT_CAPACITY_EXCEEDED"
        expected_unassigned[job.id] = reason
        actual = unassigned.get(job.id)
        if actual is None or actual.reason_code != reason:
            _validation_failed(f"job {job.id} has an invalid unassigned reason")

    expected_route_ids = [
        engineer.id
        for engineer in ordered_engineers
        if expected_by_engineer[engineer.id]
    ]
    if [route.engineer_id for route in result.routes] != expected_route_ids:
        _validation_failed("route order does not match deterministic engineer order")

    matrix_job_index = {job.id: index for index, job in enumerate(jobs)}
    matrix_engineer_index = {
        engineer.id: len(jobs) + index for index, engineer in enumerate(engineers)
    }
    expected_size = len(jobs) + len(engineers)
    total_distance = 0
    window_hits = 0
    for engineer_id in expected_route_ids:
        engineer = engineers_by_id[engineer_id]
        route = route_by_engineer[engineer_id]
        capacity = engineer.shift_end_min - engineer.shift_start_min
        if route.engineer_input_order != engineer_order[engineer_id]:
            _validation_failed(f"engineer order mismatch for route {engineer_id}")
        if route.capacity_minutes != capacity:
            _validation_failed(f"capacity mismatch for route {engineer_id}")
        if route.consumed_minutes != expected_consumed[engineer_id]:
            _validation_failed(f"consumed time mismatch for route {engineer_id}")
        if route.remaining_minutes != capacity - route.consumed_minutes:
            _validation_failed(f"remaining time mismatch for route {engineer_id}")
        if route.shift_start != _utc_at(
            data.planning_date, engineer.shift_start_min, data.timezone
        ) or route.shift_end != _utc_at(
            data.planning_date, engineer.shift_end_min, data.timezone
        ):
            _validation_failed(f"shift timestamps mismatch for route {engineer_id}")
        if [item.job_id for item in route.jobs] != expected_by_engineer[engineer_id]:
            _validation_failed(f"job sequence mismatch for route {engineer_id}")

        profile = (
            "auto" if engineer.transport_type == TransportType.CAR else "pedestrian"
        )
        matrix = distance_matrices.get(profile)
        if (
            matrix is None
            or len(matrix) != expected_size
            or any(len(row) != expected_size for row in matrix)
        ):
            _validation_failed(f"distance matrix {profile} is incomplete")
        current_minute = engineer.shift_start_min
        previous_index = matrix_engineer_index[engineer_id]
        route_distance = 0
        for position, item in enumerate(route.jobs, start=1):
            job = jobs_by_id[item.job_id]
            current_minute += BASELINE_TRAVEL_MINUTES
            planned_start = _utc_at(data.planning_date, current_minute, data.timezone)
            planned_finish = planned_start + timedelta(minutes=job.duration_min)
            distance = matrix[previous_index][matrix_job_index[job.id]]
            if distance is None or not isinstance(distance, int) or distance < 0:
                _validation_failed(f"invalid distance for job {job.id}")
            expected_hit = (
                job.window_start_min <= current_minute <= job.window_end_min
            )
            if (
                item.job_input_order != job_order[job.id]
                or item.route_position != position
                or item.received_at != (job.received_at or job.created_at)
                or item.ingest_sequence != job.ingest_sequence
                or item.service_duration_minutes != job.duration_min
                or item.planned_start != planned_start
                or item.planned_finish != planned_finish
                or item.window_from_min != job.window_start_min
                or item.window_to_min != job.window_end_min
                or item.window_hit != expected_hit
                or item.previous_location_type
                != ("ENGINEER_START" if position == 1 else "JOB")
                or item.distance_from_previous_meters != distance
            ):
                _validation_failed(f"audit fields mismatch for job {job.id}")
            route_distance += distance
            window_hits += int(expected_hit)
            current_minute += job.duration_min
            previous_index = matrix_job_index[job.id]
        if route.distance_meters != route_distance:
            _validation_failed(f"distance total mismatch for route {engineer_id}")
        total_distance += route_distance

    if [item.job_id for item in result.unassigned] != list(expected_unassigned):
        _validation_failed("unassigned job order is not deterministic")
    for item in result.unassigned:
        if item.job_input_order != job_order[item.job_id]:
            _validation_failed(f"input order mismatch for job {item.job_id}")
    assigned_count = len(assigned)
    expected_hit_rate = window_hits / assigned_count * 100 if assigned_count else None
    _, expected_earliest = baseline_applicable(data)
    scalar_mismatches = (
        result.project_id != data.project_id,
        result.planning_date != data.planning_date,
        result.algorithm_version != BASELINE_ALGORITHM_VERSION,
        result.input_hash != _input_hash(data),
        result.travel_matrix_hash != _hash(distance_matrices),
        result.input_jobs_count != len(jobs),
        result.assigned_jobs_count != assigned_count,
        result.unassigned_jobs_count != len(unassigned),
        result.window_hit_count != window_hits,
        result.window_miss_count != assigned_count - window_hits,
        result.window_hit_rate != expected_hit_rate,
        result.active_engineer_count != len(expected_route_ids),
        result.total_distance_meters != total_distance,
        result.earliest_shift_start != expected_earliest,
    )
    if any(scalar_mismatches):
        _validation_failed("baseline aggregate metrics are inconsistent")


def compare_with_optimized(
    data: PlanningInput,
    optimized: PlanningResult,
    baseline: BaselinePlanResult,
) -> BaselineComparison | None:
    if baseline.status != "READY":
        return None
    jobs = {job.id: job for job in (data.baseline_jobs or data.jobs)}
    optimized_assignments = [item for route in optimized.routes for item in route.jobs]
    optimized_ids = {item.job_id for item in optimized_assignments}
    baseline_ids = {item.job_id for route in baseline.routes for item in route.jobs}
    optimized_window_hits = sum(
        _window_hit(item.planned_start, jobs[item.job_id], data.timezone)
        for item in optimized_assignments
        if item.job_id in jobs
    )
    optimized_count = len(optimized_ids)
    optimized_active = int(
        optimized.objective_metrics.get(
            "used_engineer_count",
            len({route.engineer_id for route in optimized.routes}),
        )
    )
    optimized_distance = int(
        optimized.objective_metrics.get(
            "total_distance_meters",
            sum(route.total_distance_meters for route in optimized.routes),
        )
    )
    baseline_metrics = {
        "input_jobs_count": baseline.input_jobs_count,
        "assigned_jobs_count": baseline.assigned_jobs_count,
        "unassigned_jobs_count": baseline.unassigned_jobs_count,
        "window_hit_count": baseline.window_hit_count,
        "window_miss_count": baseline.window_miss_count,
        "window_hit_rate": baseline.window_hit_rate,
        "active_engineer_count": baseline.active_engineer_count,
        "total_distance_meters": baseline.total_distance_meters,
    }
    optimized_metrics = {
        "input_jobs_count": baseline.input_jobs_count,
        "assigned_jobs_count": optimized_count,
        "unassigned_jobs_count": max(0, baseline.input_jobs_count - optimized_count),
        "window_hit_count": optimized_window_hits,
        "window_miss_count": optimized_count - optimized_window_hits,
        "window_hit_rate": (
            optimized_window_hits / optimized_count * 100 if optimized_count else None
        ),
        "active_engineer_count": optimized_active,
        "total_distance_meters": optimized_distance,
    }
    deltas = {
        "assigned_jobs_count": optimized_count - baseline.assigned_jobs_count,
        "unassigned_jobs_count": (
            optimized_metrics["unassigned_jobs_count"] - baseline.unassigned_jobs_count
        ),
        "window_hit_count": optimized_window_hits - baseline.window_hit_count,
        "window_hit_rate": _nullable_delta(
            optimized_metrics["window_hit_rate"], baseline.window_hit_rate
        ),
        "active_engineer_count": optimized_active - baseline.active_engineer_count,
        "total_distance_meters": optimized_distance - baseline.total_distance_meters,
    }
    baseline_engineers = {route.engineer_id: route for route in baseline.routes}
    optimized_engineers = {route.engineer_id: route for route in optimized.routes}
    names = {engineer.id: engineer.name for engineer in data.engineers}
    engineer_metrics = []
    optimized_order = [
        route.engineer_id for route in optimized.routes if route.jobs
    ]
    baseline_only = sorted(
        set(baseline_engineers) - set(optimized_order),
        key=lambda engineer_id: (
            (names.get(engineer_id) or "").casefold(),
            engineer_id,
        ),
    )
    for engineer_id in [*dict.fromkeys(optimized_order), *baseline_only]:
        baseline_route = baseline_engineers.get(engineer_id)
        optimized_route = optimized_engineers.get(engineer_id)
        baseline_distance = baseline_route.distance_meters if baseline_route else 0
        optimized_route_distance = (
            optimized_route.total_distance_meters if optimized_route else 0
        )
        engineer_metrics.append(
            {
                "engineer_id": engineer_id,
                "engineer_name": (
                    names.get(engineer_id)
                    or (baseline_route.engineer_name if baseline_route else None)
                ),
                "baseline_jobs_count": len(baseline_route.jobs)
                if baseline_route
                else 0,
                "optimized_jobs_count": len(optimized_route.jobs)
                if optimized_route
                else 0,
                "baseline_distance_meters": baseline_distance,
                "optimized_distance_meters": optimized_route_distance,
                "delta_distance_meters": optimized_route_distance - baseline_distance,
            }
        )
    return BaselineComparison(
        coverage_comparable=baseline_ids == optimized_ids,
        baseline_assigned_job_ids_hash=_id_set_hash(baseline_ids),
        optimized_assigned_job_ids_hash=_id_set_hash(optimized_ids),
        baseline_metrics=baseline_metrics,
        optimized_metrics=optimized_metrics,
        deltas=deltas,
        engineer_metrics=tuple(engineer_metrics),
    )


def _job_order(job: Job) -> tuple[datetime, int, int]:
    return (job.received_at or job.created_at, int(job.ingest_sequence or 1), job.id)


def _engineer_order(engineer: Engineer) -> tuple[datetime, int]:
    created_at = engineer.created_at or datetime.min.replace(tzinfo=timezone.utc)
    return created_at, engineer.id


def _transport_matches(job: Job, engineer: Engineer) -> bool:
    return not (
        job.required_transport == TransportType.CAR
        and engineer.transport_type != TransportType.CAR
    )


def _validation_failed(message: str) -> None:
    raise BaselineCalculationError("BASELINE_VALIDATION_FAILED", message)


def _window_hit(value: datetime, job: Job, timezone_name: str) -> bool:
    local = value.astimezone(ZoneInfo(timezone_name))
    minute = local.hour * 60 + local.minute
    return job.window_start_min <= minute <= job.window_end_min


def _utc_at(planning_date: date, minute: int, timezone_name: str) -> datetime:
    local_midnight = datetime.combine(
        planning_date, time.min, tzinfo=ZoneInfo(timezone_name)
    )
    return (local_midnight + timedelta(minutes=minute)).astimezone(timezone.utc)


def _datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _nullable_delta(after: float | None, before: float | None) -> float | None:
    if after is None or before is None:
        return None
    return after - before


def _input_hash(data: PlanningInput) -> str:
    return _hash(data.snapshot)


def _id_set_hash(values: set[int]) -> str:
    return _hash(sorted(values))


def _baseline_hash_payload(result: BaselinePlanResult) -> dict[str, Any]:
    value = asdict(result)
    value.pop("result_hash", None)
    value.pop("calculation_time_ms", None)
    return value


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        items = [_jsonable(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":")
            ),
        )
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "as_tuple"):
        return str(value)
    return value
