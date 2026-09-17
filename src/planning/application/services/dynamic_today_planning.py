import hashlib
import json
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from time import monotonic
from typing import Any

from planning.application.interfaces.planning_solver import PlanningSolverFactory
from planning.application.services.future_opportunities import (
    FutureOpportunityCalendar,
    priority_group,
)
from planning.application.services.multi_day_planning import (
    _daily_source,
    _enforce_sla_hierarchy,
    _schedules_for,
)
from planning.application.services.planning_input_normalizer import (
    PlanningInputNormalizer,
)
from planning.application.services.today_route_protection import (
    TodayRouteProtectionService,
)
from planning.application.validators.planning_result import PlanningValidator


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

    async def insert_urgent_jobs(
        self,
        *,
        project_id: int,
        event_id_by_job: dict[int, int],
        planning_date: date,
        context: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], set[int]]:
        assignments = [_typed_assignment(item) for item in context["today_assignments"]]
        jobs_by_id = {int(item["id"]): item for item in context["source"]["jobs"]}
        urgent_ids = sorted(
            event_id_by_job,
            key=lambda job_id: (
                _date(jobs_by_id[job_id]["sla_date"]),
                _datetime(jobs_by_id[job_id]["created_at"]),
                job_id,
            ),
        )
        assigned_urgent: set[int] = set()
        for job_id in urgent_ids:
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
                assigned_urgent.add(job_id)
        return assignments, assigned_urgent

    async def replan_full_today(
        self,
        *,
        project_id: int,
        planning_date: date,
        context: dict[str, Any],
        excluded_job_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        assignments = [_typed_assignment(item) for item in context["today_assignments"]]
        source = context["source"]
        jobs_by_id = {int(item["id"]): item for item in source["jobs"]}
        schedules = _schedules_for(source, planning_date)
        routes: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for item in assignments:
            routes[int(item["engineer_id"])].append(item)
        protections = {}
        mutable_owner: dict[int, int] = {}
        for schedule in schedules:
            engineer_id = int(schedule["engineer_id"])
            protection = self._protection.build(
                engineer_id,
                routes[engineer_id],
                _minute(_time(schedule["shift_start"])),
                context["snapshot_time"],
                source["project"]["timezone"],
            )
            protections[engineer_id] = protection
            for item in protection.mutable:
                mutable_owner[int(item["job_id"])] = engineer_id
        protected_ids = {
            int(item["job_id"])
            for protection in protections.values()
            for item in protection.fixed
        }
        candidate_ids = set(jobs_by_id) - protected_ids - (excluded_job_ids or set())
        candidate_source = _daily_source(
            source, planning_date, candidate_ids, schedules
        )
        for engineer_row in candidate_source["engineers"]:
            engineer_id = int(engineer_row["engineer_id"])
            protection = protections[engineer_id]
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
        candidate_source["preallocated_equipment_by_engineer"] = (
            _preallocated_equipment(assignments, source, set(mutable_owner))
        )
        data = await self._normalizer.normalize(
            project_id,
            planning_date,
            source["project"]["timezone"],
            candidate_source,
        )
        opportunity_calendar = FutureOpportunityCalendar(
            source["jobs"], source, context["maximum_end"]
        )
        cascade_penalties = {
            item.id: item.drop_penalty
            + opportunity_calendar.get(item.id, planning_date, data.config).bonus
            for item in data.jobs
        }
        data = _with_sla_hierarchy(data, cascade_penalties, planning_date)
        result = await self._solver_factory.create(data.config.travel_provider).solve(
            data
        )
        errors = self._validator.validate(data, result)
        if errors:
            raise RuntimeError("; ".join(errors))
        replanned = [
            dict(item)
            for item in assignments
            if int(item["job_id"]) not in mutable_owner
        ]
        for route in result.routes:
            protection = protections[route.engineer_id]
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
                        "waiting_before_job_min": item.waiting_before_job_min,
                        "requirement_snapshot": {
                            "protection_diagnostics": list(protection.diagnostics)
                        },
                        "status": "NEW",
                        "address": job["address"],
                        "latitude": job["latitude"],
                        "longitude": job["longitude"],
                        "sla_date": _date(job["sla_date"]),
                        "work_type_id": int(job["work_type_id"]),
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
        candidates: list[tuple[tuple[Any, ...], int, list[dict[str, Any]]]] = []
        event_deadline = context.get("event_deadline_monotonic")
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
                data = _with_sla_hierarchy(data, cascade_penalties, planning_date)
                if int(new_job["id"]) not in {item.id for item in data.jobs}:
                    raise CandidateRejected("NEW_JOB_INCOMPATIBLE")
                try:
                    result = await self._solver_factory.create(
                        data.config.travel_provider
                    ).solve(data)
                except RuntimeError as error:
                    if str(error) == "OR-Tools did not return a feasible solution":
                        raise CandidateRejected("NO_FEASIBLE_ROUTE") from error
                    raise
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
                candidates.append((score, engineer_id, replacement))
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
                        "travel_matrix_hash": _matrix_hash(result.travel_matrices),
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
        await self._repository.select_candidate(
            event_id, int(new_job["id"]), winner[1]
        )
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
                "waiting_before_job_min": item.waiting_before_job_min,
                "requirement_snapshot": {
                    "protection_diagnostics": list(protection_diagnostics)
                },
                "status": "NEW",
                "address": job["address"],
                "latitude": job["latitude"],
                "longitude": job["longitude"],
                "sla_date": _date(job["sla_date"]),
                "work_type_id": int(job["work_type_id"]),
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
    return replace(
        data,
        jobs=_enforce_sla_hierarchy(penalized, decisions, data),
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
        vector.extend(
            (
                len(group_ids),
                sum(daily_penalties.get(job_id, 0) for job_id in group_ids),
            )
        )
    common = [job_id for job_id in mutable_ids if job_id in after_by_id]
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
        inversions,
        len(shifts),
        sum(shifts),
        new_delay,
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
    for key in ("planned_arrival", "planned_start", "planned_finish"):
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


def _matrix_hash(matrices: dict[str, list[list[int | None]]]) -> str | None:
    if not matrices:
        return None
    return hashlib.sha256(
        json.dumps(matrices, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
