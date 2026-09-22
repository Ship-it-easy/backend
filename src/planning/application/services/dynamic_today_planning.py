import hashlib
import json
import math
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from time import monotonic
from typing import Any

from planning.application.errors import SolverNoFeasibleSolution, SolverTimeLimit
from planning.application.interfaces.planning_solver import PlanningSolverFactory
from planning.application.services.future_opportunities import (
    FutureOpportunityCalendar,
    priority_group,
)
from planning.application.services.multi_day_planning import (
    _daily_source,
    _group_order,
    _prepare_sla_hierarchy,
    _schedules_for,
)
from planning.application.services.planning_input_normalizer import (
    PlanningInputNormalizer,
)
from planning.application.services.today_route_protection import (
    TodayRouteProtectionService,
)
from planning.application.validators.planning_result import PlanningValidator
from planning.domain.priority import WORK_PRIORITY_ORDER, work_priority_rank


class CandidateComparisonTimeout(RuntimeError):
    pass


class CandidateRejected(RuntimeError):
    pass


class DynamicTodayPlanningService:
    def __init__(
        self,
        normalizer: PlanningInputNormalizer,
        solver_factory: PlanningSolverFactory,
        validator: PlanningValidator,
        repository: Any,
    ):
        self._normalizer = normalizer
        self._solver_factory = solver_factory
        self._validator = validator
        self._repository = repository
        self._protection = TodayRouteProtectionService()

    async def insert_due_jobs(
        self,
        *,
        project_id: int,
        event_id_by_job: dict[int, int],
        planning_date: date,
        context: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], set[int]]:
        assignments = [_typed_assignment(item) for item in context["today_assignments"]]
        jobs_by_id = {int(item["id"]): item for item in context["source"]["jobs"]}
        due_job_ids = sorted(
            event_id_by_job,
            key=lambda job_id: (
                _group_order(
                    priority_group(
                        _date(jobs_by_id[job_id]["sla_date"]),
                        planning_date,
                        planning_date + timedelta(days=6),
                    )
                ),
                work_priority_rank(jobs_by_id[job_id].get("priority", "LOW")),
                _date(jobs_by_id[job_id]["sla_date"]),
                _datetime(jobs_by_id[job_id]["created_at"]),
                job_id,
            ),
        )
        assigned_due_jobs: set[int] = set()
        for job_id in due_job_ids:
            if job_id not in jobs_by_id:
                continue
            assignments, inserted = await self._insert_one(
                project_id=project_id,
                event_id=event_id_by_job[job_id],
                planning_date=planning_date,
                context=context,
                assignments=assignments,
                new_job=jobs_by_id[job_id],
                jobs_by_id=jobs_by_id,
            )
            if inserted:
                assigned_due_jobs.add(job_id)
        return assignments, assigned_due_jobs

    async def replan_full_today(
        self,
        *,
        project_id: int,
        planning_date: date,
        context: dict[str, Any],
        excluded_job_ids: set[int] | None = None,
        cancelled_job_ids: set[int] | None = None,
        cancelled_at_by_job: dict[int, datetime] | None = None,
        unavailable_engineer_ids: set[int] | None = None,
        affected_engineer_ids: set[int] | None = None,
        restoration_engineer_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        cancelled_job_ids = cancelled_job_ids or set()
        cancelled_at_by_job = cancelled_at_by_job or {}
        unavailable_engineer_ids = unavailable_engineer_ids or set()
        restoration_engineer_ids = restoration_engineer_ids or set()
        original_assignments = [
            _typed_assignment(item) for item in context["today_assignments"]
        ]
        assignments = [
            item
            for item in original_assignments
            if int(item["job_id"]) not in cancelled_job_ids
        ]
        for item in assignments:
            if (
                int(item["engineer_id"]) in unavailable_engineer_ids
                and item.get("status") == "IN_PROGRESS"
            ):
                # Until an actual completion arrives, the executing job remains
                # a fixed fact through the later of its planned finish and T0.
                # The adjusted finish also forms the travel-chain boundary for
                # a partially shortened shift.
                item["planned_finish"] = max(
                    item["planned_finish"], context["snapshot_time"]
                )
        source = context["source"]
        jobs_by_id = {int(item["id"]): item for item in source["jobs"]}
        schedules = _schedules_for(source, planning_date)
        routes: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for item in assignments:
            routes[int(item["engineer_id"])].append(item)
        original_routes: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for item in original_assignments:
            original_routes[int(item["engineer_id"])].append(item)
        protections = {}
        mutable_owner: dict[int, int] = {}
        protected_ids: set[int] = set()
        for schedule in schedules:
            engineer_id = int(schedule["engineer_id"])
            if (
                affected_engineer_ids is not None
                and engineer_id not in affected_engineer_ids
            ):
                protected_ids.update(
                    int(item["job_id"]) for item in routes[engineer_id]
                )
                continue
            cancelled_on_route = [
                item
                for item in original_routes[engineer_id]
                if int(item["job_id"]) in cancelled_job_ids
            ]
            pre_cancel_route = [
                {
                    **item,
                    "status": item.get("previous_status") or item.get("status"),
                }
                for item in original_routes[engineer_id]
            ]
            protected_before_cancel = self._protection.build(
                engineer_id,
                pre_cancel_route,
                _minute(_time(schedule["shift_start"])),
                context["snapshot_time"],
                source["project"]["timezone"],
            )
            cancelled_in_progress = next(
                (
                    item
                    for item in cancelled_on_route
                    if item.get("previous_status") == "IN_PROGRESS"
                ),
                None,
            )
            protected_before_ids = {
                int(item["job_id"]) for item in protected_before_cancel.fixed
            }
            cancelled_en_route = next(
                (
                    item
                    for item in cancelled_on_route
                    if item.get("previous_status") == "NEW"
                    and int(item["job_id"]) in protected_before_ids
                ),
                None,
            )
            virtual_boundary = cancelled_in_progress or cancelled_en_route
            protection = self._protection.build(
                engineer_id,
                routes[engineer_id],
                _minute(_time(schedule["shift_start"])),
                context["snapshot_time"],
                source["project"]["timezone"],
                protect_next=engineer_id not in unavailable_engineer_ids
                and virtual_boundary is None,
            )
            if virtual_boundary is not None:
                cancelled_job_id = int(virtual_boundary["job_id"])
                boundary_time = cancelled_at_by_job.get(
                    cancelled_job_id, context["snapshot_time"]
                )
                if virtual_boundary.get("previous_status") != "IN_PROGRESS":
                    boundary_time = max(
                        boundary_time, virtual_boundary["planned_start"]
                    )
                protection = replace(
                    protection,
                    boundary=virtual_boundary,
                    boundary_time=boundary_time,
                    diagnostics=(
                        *protection.diagnostics,
                        "CANCELLED_EN_ROUTE_ASSUMPTION",
                    ),
                )
            protections[engineer_id] = protection
            for item in protection.mutable:
                if engineer_id not in unavailable_engineer_ids:
                    mutable_owner[int(item["job_id"])] = engineer_id
        protected_ids.update(
            int(item["job_id"])
            for protection in protections.values()
            for item in protection.fixed
        )
        candidate_ids = set(jobs_by_id) - protected_ids - (excluded_job_ids or set())
        # A current-day recalculation may use only free backlog and today's
        # mutable assignments. Jobs already assigned on a future date stay in
        # the future cascade and must never be pulled into today's route.
        future_assigned_ids = {
            int(item["job_id"])
            for item in context["current_assignments"]
            if _date(item["planning_date"]) > planning_date
        }
        candidate_ids.difference_update(future_assigned_ids)
        assigned_elsewhere = {
            int(item["job_id"])
            for item in assignments
            if int(item["engineer_id"]) in unavailable_engineer_ids
            and item.get("status") in {"COMPLETED", "IN_PROGRESS"}
        }
        candidate_ids.difference_update(assigned_elsewhere)
        candidate_source = _daily_source(
            source, planning_date, candidate_ids, schedules
        )
        for engineer_row in candidate_source["engineers"]:
            engineer_id = int(engineer_row["engineer_id"])
            protection = protections.get(engineer_id)
            if protection is None:
                continue
            if protection.boundary is not None:
                boundary = protection.boundary
                engineer_row.update(
                    start_address=boundary.get("address"),
                    start_latitude=boundary.get("latitude"),
                    start_longitude=boundary.get("longitude"),
                    shift_start=protection.boundary_time.astimezone(
                        __import__("zoneinfo").ZoneInfo(source["project"]["timezone"])
                    )
                    .time()
                    .replace(second=0, microsecond=0),
                )
        for item in candidate_source["jobs"]:
            owner = mutable_owner.get(int(item["id"]))
            if owner is not None:
                item["allowed_engineer_ids"] = [owner]
            elif affected_engineer_ids is not None:
                item["allowed_engineer_ids"] = sorted(affected_engineer_ids)
            elif restoration_engineer_ids:
                item["allowed_engineer_ids"] = sorted(restoration_engineer_ids)
        released_assignment_ids = set(mutable_owner) | {
            int(item["job_id"])
            for item in assignments
            if int(item["engineer_id"]) in unavailable_engineer_ids
            and item.get("status") == "NEW"
        }
        candidate_source["preallocated_equipment_by_engineer"] = (
            _preallocated_equipment(assignments, source, released_assignment_ids)
        )
        data = await self._normalizer.normalize(
            project_id,
            planning_date,
            source["project"]["timezone"],
            candidate_source,
            snapshot_time=context["snapshot_time"],
        )
        data = replace(
            data,
            fixed_active_engineer_ids=frozenset(
                {
                    engineer_id
                    for engineer_id, protection in protections.items()
                    if any(
                        item.get("status") in {"NEW", "IN_PROGRESS"}
                        for item in protection.fixed
                    )
                }
                | {
                    int(item["engineer_id"])
                    for item in assignments
                    if int(item["engineer_id"]) in unavailable_engineer_ids
                    and item.get("status") == "IN_PROGRESS"
                }
            ),
        )
        opportunity_calendar = FutureOpportunityCalendar(
            source["jobs"], source, context["maximum_end"]
        )
        cascade_penalties = {
            item.id: item.drop_penalty
            + opportunity_calendar.get(item.id, planning_date, data.config).bonus
            for item in data.jobs
        }
        data = _with_event_snapshot(data, context, assignments, released_assignment_ids)
        data = _with_sla_hierarchy(data, cascade_penalties, planning_date)
        result = await self._solver_factory.create(data.config.travel_provider).solve(
            data
        )
        errors = self._validator.validate(data, result)
        if errors:
            raise RuntimeError("; ".join(errors))
        context.setdefault("today_solver_runs", []).append((data, result))
        replanned = [
            dict(item)
            for item in assignments
            if int(item["job_id"]) not in mutable_owner
            and not (
                int(item["engineer_id"]) in unavailable_engineer_ids
                and item.get("status") == "NEW"
            )
        ]
        for route in result.routes:
            protection = protections.get(route.engineer_id)
            if protection is None:
                protection = self._protection.build(
                    route.engineer_id,
                    [],
                    0,
                    context["snapshot_time"],
                    source["project"]["timezone"],
                )
            offset = len(protection.fixed)
            for item in route.jobs:
                job = jobs_by_id[item.job_id]
                replanned.append(
                    {
                        "job_id": item.job_id,
                        "planning_date": planning_date,
                        "engineer_id": route.engineer_id,
                        "sequence": offset + item.sequence,
                        "planned_arrival": item.planned_arrival,
                        "planned_start": item.planned_start,
                        "planned_finish": item.planned_finish,
                        "travel_from_previous_min": item.travel_from_previous_min,
                        "distance_from_previous_meters": (
                            item.distance_from_previous_meters
                        ),
                        "waiting_before_job_min": item.waiting_before_job_min,
                        "requirement_snapshot": {
                            "protection_diagnostics": list(protection.diagnostics),
                            "replan_boundary": (
                                {
                                    "source_job_id": int(protection.boundary["job_id"]),
                                    "model_start_time": route.planned_start.isoformat(),
                                }
                                if item.sequence == 1
                                and protection.boundary is not None
                                else None
                            ),
                            "virtual_start": (
                                {
                                    "reason": "CANCELLED_EN_ROUTE_ASSUMPTION",
                                    "source_job_id": int(protection.boundary["job_id"]),
                                    "start_time": protection.boundary_time.isoformat(),
                                }
                                if item.sequence == 1
                                and protection.boundary is not None
                                and "CANCELLED_EN_ROUTE_ASSUMPTION"
                                in protection.diagnostics
                                else None
                            ),
                        },
                        "status": "NEW",
                        "address": job["address"],
                        "latitude": job["latitude"],
                        "longitude": job["longitude"],
                        "sla_date": _date(job["sla_date"]),
                        "work_type_id": int(job["work_type_id"]),
                        "priority": job.get("priority", "LOW"),
                    }
                )
        return sorted(
            replanned,
            key=lambda item: (
                item["planning_date"],
                int(item["engineer_id"]),
                int(item["sequence"]),
            ),
        )

    async def _insert_one(
        self,
        *,
        project_id: int,
        event_id: int,
        planning_date: date,
        context: dict[str, Any],
        assignments: list[dict[str, Any]],
        new_job: dict[str, Any],
        jobs_by_id: dict[int, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        source = context["source"]
        schedules = _schedules_for(source, planning_date)
        schedule_by_engineer = {int(item["engineer_id"]): item for item in schedules}
        engineer_by_id = {int(item["id"]): item for item in source["engineers"]}
        routes: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for item in assignments:
            routes[int(item["engineer_id"])].append(item)
        today_new_ids = {
            int(item["job_id"]) for item in assignments if item["status"] == "NEW"
        } | {int(new_job["id"])}
        common_source = _daily_source(source, planning_date, today_new_ids, schedules)
        common_data = await self._normalizer.normalize(
            project_id,
            planning_date,
            source["project"]["timezone"],
            common_source,
            snapshot_time=context["snapshot_time"],
        )
        daily_penalties = {item.id: item.drop_penalty for item in common_data.jobs}
        opportunity_calendar = FutureOpportunityCalendar(
            source["jobs"], source, context["maximum_end"]
        )
        cascade_penalties = {
            item.id: item.drop_penalty
            + opportunity_calendar.get(item.id, planning_date, common_data.config).bonus
            for item in common_data.jobs
        }
        candidates: list[
            tuple[tuple[Any, ...], int, list[dict[str, Any]], Any, Any]
        ] = []
        event_deadline = context.get("event_deadline_monotonic") or (
            monotonic() + common_data.config.event_time_limit_sec
        )
        context["event_deadline_monotonic"] = event_deadline
        for item in assignments:
            item.setdefault(
                "priority",
                jobs_by_id.get(int(item["job_id"]), {}).get("priority", "LOW"),
            )
        candidate_engineer_ids = sorted(schedule_by_engineer)
        await self._repository.set_candidate_progress(
            event_id,
            total=len(candidate_engineer_ids),
            completed=0,
            current_engineer_id=None,
        )
        for completed, engineer_id in enumerate(candidate_engineer_ids):
            if event_deadline is not None and monotonic() >= event_deadline:
                raise CandidateComparisonTimeout("CANDIDATE_COMPARISON_TIMEOUT")
            await self._repository.set_candidate_progress(
                event_id,
                total=len(candidate_engineer_ids),
                completed=completed,
                current_engineer_id=engineer_id,
            )
            engineer = engineer_by_id.get(engineer_id)
            if engineer is None:
                await self._repository.set_candidate_progress(
                    event_id,
                    total=len(candidate_engineer_ids),
                    completed=completed + 1,
                    current_engineer_id=None,
                )
                continue
            schedule = schedule_by_engineer[engineer_id]
            protection = self._protection.build(
                engineer_id,
                routes[engineer_id],
                _minute(_time(schedule["shift_start"])),
                context["snapshot_time"],
                source["project"]["timezone"],
            )
            mutable_ids = {int(item["job_id"]) for item in protection.mutable}
            selected_ids = mutable_ids | {int(new_job["id"])}
            candidate_source = _daily_source(
                source, planning_date, selected_ids, [schedule]
            )
            boundary = protection.boundary
            candidate_engineer = candidate_source["engineers"][0]
            if boundary is not None:
                candidate_engineer.update(
                    start_address=boundary.get("address"),
                    start_latitude=boundary.get("latitude"),
                    start_longitude=boundary.get("longitude"),
                    shift_start=protection.boundary_time.astimezone(
                        __import__("zoneinfo").ZoneInfo(source["project"]["timezone"])
                    )
                    .time()
                    .replace(second=0, microsecond=0),
                )
            for item in candidate_source["jobs"]:
                item["allowed_engineer_ids"] = [engineer_id]
                item["mandatory"] = int(item["id"]) == int(new_job["id"])
            candidate_source["preallocated_equipment_by_engineer"] = (
                _preallocated_equipment(assignments, source, mutable_ids)
            )
            try:
                data = await self._normalizer.normalize(
                    project_id,
                    planning_date,
                    source["project"]["timezone"],
                    candidate_source,
                    snapshot_time=context["snapshot_time"],
                )
                data = replace(
                    data,
                    fixed_active_engineer_ids=frozenset(
                        {engineer_id}
                        if any(
                            item.get("status") in {"NEW", "IN_PROGRESS"}
                            for item in protection.fixed
                        )
                        else set()
                    ),
                )
                if event_deadline is None:
                    event_deadline = monotonic() + data.config.event_time_limit_sec
                if monotonic() >= event_deadline:
                    raise CandidateComparisonTimeout("CANDIDATE_COMPARISON_TIMEOUT")
                data = replace(
                    data,
                    config=replace(
                        data.config,
                        solver_time_limit_sec=(
                            data.config.candidate_solver_time_limit_sec
                        ),
                    ),
                )
                data = _with_event_snapshot(data, context, assignments, mutable_ids)
                data = _with_sla_hierarchy(data, cascade_penalties, planning_date)
                if int(new_job["id"]) not in {item.id for item in data.jobs}:
                    raise CandidateRejected("NEW_JOB_INCOMPATIBLE")
                try:
                    result = await self._solver_factory.create(
                        data.config.travel_provider
                    ).solve(data)
                except SolverNoFeasibleSolution as error:
                    raise CandidateRejected("NO_FEASIBLE_ROUTE") from error
                except SolverTimeLimit as error:
                    raise CandidateComparisonTimeout(
                        "CANDIDATE_COMPARISON_TIMEOUT"
                    ) from error
                if monotonic() >= event_deadline:
                    raise CandidateComparisonTimeout("CANDIDATE_COMPARISON_TIMEOUT")
                errors = self._validator.validate(data, result)
                route = next(
                    (item for item in result.routes if item.engineer_id == engineer_id),
                    None,
                )
                if (
                    errors
                    or route is None
                    or int(new_job["id"]) not in {item.job_id for item in route.jobs}
                ):
                    raise CandidateRejected(
                        "; ".join(errors) or "MANDATORY_JOB_NOT_ASSIGNED"
                    )
                replacement = _replace_route(
                    assignments,
                    engineer_id,
                    mutable_ids,
                    protection.fixed,
                    protection.diagnostics,
                    route,
                    planning_date,
                    jobs_by_id,
                )
                score = _candidate_score(
                    assignments,
                    replacement,
                    mutable_ids,
                    int(new_job["id"]),
                    context["snapshot_time"],
                    planning_date,
                    daily_penalties,
                )
                candidates.append((score, engineer_id, replacement, data, result))
                await self._repository.save_candidate(
                    {
                        "project_id": project_id,
                        "planning_event_id": event_id,
                        "subject_job_id": int(new_job["id"]),
                        "engineer_id": engineer_id,
                        "planning_date": planning_date,
                        "solver_status": result.solver_status,
                        "validator_status": "VALID",
                        "original_route": [
                            int(item["job_id"]) for item in routes[engineer_id]
                        ],
                        "result_route": [item.job_id for item in route.jobs],
                        "dropped_job_ids": sorted(
                            mutable_ids - {item.job_id for item in route.jobs}
                        ),
                        "score_vector": list(score),
                        "travel_matrix_hash": _matrix_hash(
                            result.travel_time_seconds_matrices,
                            result.distance_matrices,
                        ),
                        "solver_time_ms": result.solver_time_ms,
                    }
                )
            except CandidateComparisonTimeout:
                raise
            except CandidateRejected as error:
                await self._repository.save_candidate(
                    {
                        "project_id": project_id,
                        "planning_event_id": event_id,
                        "subject_job_id": int(new_job["id"]),
                        "engineer_id": engineer_id,
                        "planning_date": planning_date,
                        "solver_status": "REJECTED",
                        "validator_status": "INVALID",
                        "rejection_reason": str(error)[:128],
                        "original_route": [
                            int(item["job_id"]) for item in routes[engineer_id]
                        ],
                        "result_route": [],
                        "dropped_job_ids": [],
                        "solver_time_ms": 0,
                    }
                )
            await self._repository.set_candidate_progress(
                event_id,
                total=len(candidate_engineer_ids),
                completed=completed + 1,
                current_engineer_id=None,
            )
        if not candidates:
            return assignments, False
        winner = min(candidates, key=lambda item: item[0])
        await self._repository.select_candidate(event_id, int(new_job["id"]), winner[1])
        context.setdefault("today_solver_runs", []).append((winner[3], winner[4]))
        return winner[2], True


def _replace_route(
    assignments: list[dict[str, Any]],
    engineer_id: int,
    mutable_ids: set[int],
    fixed: tuple[dict[str, Any], ...],
    protection_diagnostics: tuple[str, ...],
    route: Any,
    planning_date: date,
    jobs_by_id: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    result = [
        dict(item)
        for item in assignments
        if not (
            int(item["engineer_id"]) == engineer_id
            and int(item["job_id"]) in mutable_ids
        )
    ]
    offset = len(fixed)
    for item in route.jobs:
        job = jobs_by_id[item.job_id]
        result.append(
            {
                "job_id": item.job_id,
                "planning_date": planning_date,
                "engineer_id": engineer_id,
                "sequence": offset + item.sequence,
                "planned_arrival": item.planned_arrival,
                "planned_start": item.planned_start,
                "planned_finish": item.planned_finish,
                "travel_from_previous_min": item.travel_from_previous_min,
                "distance_from_previous_meters": item.distance_from_previous_meters,
                "waiting_before_job_min": item.waiting_before_job_min,
                "requirement_snapshot": {
                    "protection_diagnostics": list(protection_diagnostics),
                    "replan_boundary": (
                        {
                            "source_job_id": int(fixed[-1]["job_id"]),
                            "model_start_time": route.planned_start.isoformat(),
                        }
                        if item.sequence == 1 and fixed
                        else None
                    ),
                },
                "status": "NEW",
                "address": job["address"],
                "latitude": job["latitude"],
                "longitude": job["longitude"],
                "sla_date": _date(job["sla_date"]),
                "work_type_id": int(job["work_type_id"]),
                "priority": job.get("priority", "LOW"),
            }
        )
    return sorted(
        result,
        key=lambda item: (
            item["planning_date"],
            int(item["engineer_id"]),
            int(item["sequence"]),
        ),
    )


def _with_event_snapshot(data, context, assignments, mutable_ids):
    fixed_legs: dict[int, list[int]] = defaultdict(list)
    for item in assignments:
        if int(item["job_id"]) not in mutable_ids:
            fixed_legs[int(item["engineer_id"])].append(
                int(item.get("distance_from_previous_meters") or 0)
            )
    legs = {key: tuple(values) for key, values in fixed_legs.items()}
    return replace(
        data,
        fixed_active_engineer_ids=frozenset(
            int(item["engineer_id"])
            for item in assignments
            if int(item["job_id"]) not in mutable_ids
            and item.get("status") in {"NEW", "IN_PROGRESS"}
        ),
        fixed_distance_legs_by_engineer=legs,
        travel_snapshot=context.setdefault("travel_snapshot", {}),
        solve_deadline_monotonic=context.get("event_deadline_monotonic"),
        snapshot={**data.snapshot, "fixed_distance_legs_by_engineer": legs},
    )


def _with_sla_hierarchy(
    data: Any,
    cascade_penalties: dict[int, int],
    planning_date: date,
) -> Any:
    decisions = {
        item.id: {
            "priority_group": priority_group(
                item.sla_date,
                planning_date,
                planning_date + timedelta(days=6),
            ),
            "cascade_drop_penalty": cascade_penalties.get(item.id, item.drop_penalty),
        }
        for item in data.jobs
    }
    penalized = [
        replace(
            item,
            drop_penalty=decisions[item.id]["cascade_drop_penalty"],
        )
        for item in data.jobs
    ]
    adjusted, drop_priority_stages, penalty_encoding = _prepare_sla_hierarchy(
        penalized, decisions, data
    )
    positive_costs = [item.drop_penalty for item in adjusted if item.drop_penalty > 0]
    divisor = math.gcd(*positive_costs) if positive_costs else 1
    penalty_components = {
        str(job_id): dict(values)
        for job_id, values in data.snapshot.get("penalty_components", {}).items()
    }
    adjusted_by_id = {item.id: item for item in adjusted}
    for item in data.jobs:
        values = penalty_components.setdefault(str(item.id), {})
        daily_penalty = int(values.get("daily_drop_penalty_v2", item.drop_penalty))
        cascade_penalty = int(cascade_penalties.get(item.id, item.drop_penalty))
        values.update(
            future_opportunity_bonus=max(0, cascade_penalty - daily_penalty),
            cascade_drop_penalty_v2=cascade_penalty,
            solver_drop_cost=adjusted_by_id[item.id].drop_penalty // divisor,
        )
    return replace(
        data,
        jobs=adjusted,
        snapshot={
            **data.snapshot,
            "drop_priority_stages": drop_priority_stages,
            "penalty_encoding": penalty_encoding,
            "penalty_components": penalty_components,
            "sla_hierarchy_version": "sla-work-priority-count-v2",
            "jobs": [
                {**item, "drop_penalty": adjusted_by_id[item["id"]].drop_penalty}
                if item["id"] in adjusted_by_id
                else item
                for item in data.snapshot.get("jobs", [])
            ],
        },
    )


def _candidate_score(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    mutable_ids: set[int],
    new_job_id: int,
    snapshot_time: datetime,
    planning_date: date,
    daily_penalties: dict[int, int],
) -> tuple[Any, ...]:
    before_by_id = {int(item["job_id"]): item for item in before}
    after_by_id = {int(item["job_id"]): item for item in after}
    groups = ("OVERDUE", "DUE_TODAY", "DUE_1", "DUE_2_3", "DUE_LATER")
    dropped = mutable_ids - set(after_by_id)
    vector: list[int] = []
    for group in groups:
        group_ids = {
            job_id
            for job_id in dropped
            if _sla_group(_date(before_by_id[job_id]["sla_date"]), planning_date)
            == group
        }
        for priority in WORK_PRIORITY_ORDER:
            category_ids = {
                job_id
                for job_id in group_ids
                if before_by_id[job_id].get("priority", "LOW") == priority.value
            }
            vector.extend(
                (
                    len(category_ids),
                    sum(daily_penalties.get(job_id, 0) for job_id in category_ids),
                )
            )
    common = [job_id for job_id in mutable_ids if job_id in after_by_id]
    used_engineers = len(
        {
            int(item["engineer_id"])
            for item in after
            if item.get("status") in {"NEW", "IN_PROGRESS"}
        }
    )
    inversions = 0
    for index, left in enumerate(common):
        for right in common[index + 1 :]:
            old_order = before_by_id[left]["sequence"] < before_by_id[right]["sequence"]
            new_order = after_by_id[left]["sequence"] < after_by_id[right]["sequence"]
            inversions += old_order != new_order
    shifts = [
        abs(
            int(
                (
                    after_by_id[job_id]["planned_start"]
                    - before_by_id[job_id]["planned_start"]
                ).total_seconds()
                // 60
            )
        )
        for job_id in common
        if after_by_id[job_id]["planned_start"] != before_by_id[job_id]["planned_start"]
    ]
    new_delay = max(
        0,
        int(
            (after_by_id[new_job_id]["planned_start"] - snapshot_time).total_seconds()
            // 60
        ),
    )
    travel = sum(int(item.get("travel_from_previous_min") or 0) for item in after)
    distance_by_engineer: dict[int, int] = defaultdict(int)
    for item in after:
        distance_by_engineer[int(item["engineer_id"])] += int(
            item.get("distance_from_previous_meters") or 0
        )
    total_distance = sum(distance_by_engineer.values())
    max_distance = max(distance_by_engineer.values(), default=0)
    waiting = sum(int(item.get("waiting_before_job_min") or 0) for item in after)
    engineer_id = int(after_by_id[new_job_id]["engineer_id"])
    tie_route = tuple(
        value
        for item in sorted(
            after,
            key=lambda value: (
                int(value["engineer_id"]),
                int(value["sequence"]),
                int(value["job_id"]),
            ),
        )
        for value in (
            int(item["engineer_id"]),
            int(item["sequence"]),
            int(item["job_id"]),
        )
    )
    return (
        *vector,
        used_engineers,
        inversions,
        len(shifts),
        sum(shifts),
        new_delay,
        total_distance,
        max_distance,
        travel,
        waiting,
        engineer_id,
        *tie_route,
    )


def _preallocated_equipment(
    assignments: list[dict[str, Any]],
    source: dict[str, Any],
    mutable_ids: set[int],
) -> dict[int, set[int]]:
    required = {
        int(key): {int(value) for value in values}
        for key, values in source["required_equipment"].items()
    }
    result: dict[int, set[int]] = defaultdict(set)
    for item in assignments:
        if int(item["job_id"]) in mutable_ids:
            continue
        result[int(item["engineer_id"])].update(
            required.get(int(item["work_type_id"]), set())
        )
    return result


def _typed_assignment(item: dict[str, Any]) -> dict[str, Any]:
    value = dict(item)
    for key in (
        "planned_arrival",
        "planned_start",
        "planned_finish",
        "actual_started_at",
        "actual_completed_at",
    ):
        if value.get(key) is not None:
            value[key] = _datetime(value[key])
    value["planning_date"] = _date(value["planning_date"])
    value["sla_date"] = _date(value["sla_date"])
    return value


def _sla_group(sla_date: date, planning_date: date) -> str:
    days = (sla_date - planning_date).days
    if days < 0:
        return "OVERDUE"
    if days == 0:
        return "DUE_TODAY"
    if days == 1:
        return "DUE_1"
    if days <= 3:
        return "DUE_2_3"
    return "DUE_LATER"


def _minute(value: time) -> int:
    return value.hour * 60 + value.minute


def _date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _time(value: time | str) -> time:
    return value if isinstance(value, time) else time.fromisoformat(value)


def _datetime(value: datetime | str) -> datetime:
    return (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _matrix_hash(
    time_matrices: dict[str, list[list[int | None]]],
    distance_matrices: dict[str, list[list[int | None]]],
) -> str | None:
    if not time_matrices:
        return None
    return hashlib.sha256(
        json.dumps(
            {
                "travel_time_seconds": time_matrices,
                "distance_meters": distance_matrices,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
