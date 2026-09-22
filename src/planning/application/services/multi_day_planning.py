import math
import time as monotonic_time
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from planning.application.interfaces.planning_batch_repository import (
    PlanningBatchRepository,
)
from planning.application.interfaces.planning_solver import PlanningSolverFactory
from planning.application.services.future_opportunities import (
    FutureOpportunityCalendar,
    permanent_issue,
    priority_group,
)
from planning.application.services.planning_input_normalizer import (
    PlanningInputNormalizer,
)
from planning.application.validators.planning_batch import PlanningBatchValidator
from planning.application.validators.planning_result import PlanningValidator
from planning.domain.entities.job import UnassignedJob
from planning.domain.entities.planning import (
    PlanningConfig,
    PlanningInput,
    PlanningResult,
)
from planning.domain.enums import ReasonCode
from planning.domain.priority import (
    work_priority,
    work_priority_bonus,
    work_priority_rank,
)


class MultiDayPlanningService:
    def __init__(
        self,
        repository: PlanningBatchRepository,
        normalizer: PlanningInputNormalizer,
        solver_factory: PlanningSolverFactory,
        daily_validator: PlanningValidator,
        batch_validator: PlanningBatchValidator,
    ):
        self._repository = repository
        self._normalizer = normalizer
        self._solver_factory = solver_factory
        self._daily_validator = daily_validator
        self._batch_validator = batch_validator

    async def execute(self, batch_id: int) -> None:
        batch = await self._repository.load_for_execution(batch_id)
        if batch is None or batch["status"] not in {
            "CREATED",
            "PREPARING",
            "RUNNING",
            "STOP_REQUESTED",
        }:
            return
        snapshot = batch["input_snapshot"]
        effective_start = _date(batch["effective_start_date"])
        maximum_end = _date(batch["maximum_horizon_end"])
        config = PlanningConfig(**snapshot["config"])
        all_jobs = {int(item["id"]): item for item in snapshot["jobs"]}
        opportunity_calendar = FutureOpportunityCalendar(
            list(all_jobs.values()), snapshot, maximum_end
        )
        states = batch.get("execution_job_states", [])
        remaining = {
            int(item["job_id"])
            for item in states
            if item["processing_status"] == "BACKLOG"
        }
        if not states:
            remaining = set(all_jobs)
        assigned_sequence = [
            int(item["job_id"])
            for item in states
            if item["processing_status"] == "DRAFT_ASSIGNED"
        ]
        assignment_dates = {
            int(item["job_id"]): _date(item["assigned_date"])
            for item in states
            if item["processing_status"] == "DRAFT_ASSIGNED"
            and item.get("assigned_date")
        }
        existing_permanent = sum(
            1 for item in states if item["processing_status"] == "PERMANENT_ISSUE"
        )
        persisted_started_at = _datetime(batch.get("started_at"))
        elapsed_before_restart = (
            max(
                0.0,
                (datetime.now(timezone.utc) - persisted_started_at).total_seconds(),
            )
            if persisted_started_at is not None
            else 0.0
        )
        started = monotonic_time.monotonic() - elapsed_before_restart
        day_states = batch.get("execution_day_states", [])
        processed = sum(
            1
            for item in day_states
            if item["status"] in {"SUCCESS", "SKIPPED_NO_SHIFT"}
        )
        successful = sum(1 for item in day_states if item["status"] == "SUCCESS")
        skipped = sum(1 for item in day_states if item["status"] == "SKIPPED_NO_SHIFT")
        if batch["status"] == "STOP_REQUESTED":
            await self._finish(
                batch_id,
                "PARTIAL",
                "STOPPED_BY_USER",
                remaining,
                started,
                processed,
                successful,
                skipped,
                assigned_sequence,
                existing_permanent,
            )
            return
        await self._repository.mark_batch_status(batch_id, "PREPARING")
        issues = {
            job_id: issue
            for job_id, job in all_jobs.items()
            if job_id in remaining
            if (issue := permanent_issue(job, snapshot, effective_start, maximum_end))
            is not None
        }
        if issues:
            await self._repository.set_permanent_issues(batch_id, issues)
            remaining.difference_update(issues)
        if not all_jobs:
            await self._finish(
                batch_id,
                "SUCCESS",
                "NO_ELIGIBLE_JOBS",
                remaining,
                started,
                processed,
                successful,
                skipped,
                assigned_sequence,
                existing_permanent + len(issues),
            )
            return
        if not remaining:
            permanent_count = existing_permanent + len(issues)
            await self._finish(
                batch_id,
                "SUCCESS" if permanent_count == 0 else "PARTIAL",
                (
                    "ALL_ELIGIBLE_ASSIGNED"
                    if permanent_count == 0
                    else "NO_FUTURE_OPPORTUNITIES"
                ),
                remaining,
                started,
                processed,
                successful,
                skipped,
                assigned_sequence,
                permanent_count,
            )
            return
        await self._repository.mark_batch_status(batch_id, "RUNNING")

        current = (
            _date(batch["processed_through_date"]) + timedelta(days=1)
            if batch.get("processed_through_date")
            else effective_start
        )
        while current <= maximum_end and remaining:
            if await self._repository.should_stop(batch_id):
                await self._finish(
                    batch_id,
                    "PARTIAL",
                    "STOPPED_BY_USER",
                    remaining,
                    started,
                    processed,
                    successful,
                    skipped,
                    assigned_sequence,
                    existing_permanent + len(issues),
                )
                return
            if (
                monotonic_time.monotonic() - started
                >= config.batch_total_time_limit_sec
            ):
                await self._finish(
                    batch_id,
                    "PARTIAL",
                    "TOTAL_TIME_LIMIT",
                    remaining,
                    started,
                    processed,
                    successful,
                    skipped,
                    assigned_sequence,
                    existing_permanent + len(issues),
                )
                return
            if (current - effective_start).days % 7 == 0:
                block_dates = [
                    current + timedelta(days=offset)
                    for offset in range(7)
                    if current + timedelta(days=offset) <= maximum_end
                ]
                await self._repository.create_days(batch_id, block_dates)

            opportunities = {
                job_id: opportunity_calendar.get(job_id, current, config)
                for job_id in remaining
            }
            exhausted = {
                job_id: permanent_issue(
                    all_jobs[job_id], snapshot, current, maximum_end
                )
                or (
                    "NO_COMPATIBLE_ENGINEER_IN_HORIZON",
                    {"checked_from": current.isoformat()},
                )
                for job_id, value in opportunities.items()
                if value.count == 0
            }
            if exhausted:
                await self._repository.set_permanent_issues(batch_id, exhausted)
                issues.update(exhausted)
                remaining.difference_update(exhausted)
            if not remaining:
                await self._finish(
                    batch_id,
                    "PARTIAL",
                    "NO_FUTURE_OPPORTUNITIES",
                    remaining,
                    started,
                    processed,
                    successful,
                    skipped,
                    assigned_sequence,
                    existing_permanent + len(issues),
                )
                return

            schedules = _schedules_for(snapshot, current)
            if not schedules:
                await self._repository.skip_day(batch_id, current, len(remaining))
                processed += 1
                skipped += 1
                current += timedelta(days=1)
                continue

            daily_source = _daily_source(snapshot, current, remaining, schedules)
            daily_limit = config.max_jobs_per_run
            daily_source["config"]["max_jobs_per_run"] = max(
                daily_limit, len(remaining)
            )
            data = await self._normalizer.normalize(
                int(batch["project_id"]),
                current,
                batch["input_snapshot"]["project"]["timezone"],
                daily_source,
            )
            newly_permanent = {
                item.job_id: (item.reason_code.value, item.diagnostic_flags)
                for item in data.pre_unassigned
                if item.reason_code
                in {
                    ReasonCode.INVALID_INPUT,
                    ReasonCode.MISSING_SERVICE_DURATION,
                    ReasonCode.GEOCODING_FAILED,
                }
            }
            if newly_permanent:
                await self._repository.set_permanent_issues(batch_id, newly_permanent)
                issues.update(newly_permanent)
                remaining.difference_update(newly_permanent)
                data = replace(
                    data,
                    pre_unassigned=[
                        item
                        for item in data.pre_unassigned
                        if item.job_id not in newly_permanent
                    ],
                    input_jobs_count=data.input_jobs_count - len(newly_permanent),
                )
            if not remaining:
                await self._finish(
                    batch_id,
                    "PARTIAL",
                    "NO_FUTURE_OPPORTUNITIES",
                    remaining,
                    started,
                    processed,
                    successful,
                    skipped,
                    assigned_sequence,
                    existing_permanent + len(issues),
                )
                return
            if (
                monotonic_time.monotonic() - started
                >= config.batch_total_time_limit_sec
            ):
                await self._finish(
                    batch_id,
                    "PARTIAL",
                    "TOTAL_TIME_LIMIT",
                    remaining,
                    started,
                    processed,
                    successful,
                    skipped,
                    assigned_sequence,
                    existing_permanent + len(issues),
                )
                return

            block_number = (current - effective_start).days // 7
            block_end = min(
                effective_start + timedelta(days=(block_number + 1) * 7 - 1),
                maximum_end,
            )
            decisions: dict[int, dict[str, Any]] = {}
            for item in data.snapshot["jobs"]:
                job_id = int(item["id"])
                if job_id not in remaining:
                    continue
                opportunity = opportunities[job_id]
                daily_penalty = int(item.get("drop_penalty", 0))
                decisions[job_id] = {
                    "priority_group": priority_group(
                        _date(item["sla_date"]), current, block_end
                    ),
                    "future_opportunity_count": opportunity.count,
                    "future_opportunity_rank": opportunity.rank,
                    "future_opportunity_bonus": opportunity.bonus,
                    "daily_drop_penalty": daily_penalty,
                    "cascade_drop_penalty": daily_penalty + opportunity.bonus,
                    "priority_bonus": work_priority_bonus(
                        item.get("priority", "LOW")
                    ),
                }
            penalized = []
            for job in data.jobs:
                penalized.append(
                    replace(
                        job,
                        drop_penalty=decisions[job.id]["cascade_drop_penalty"],
                    )
                )
            ordered = _order_for_daily_limit(penalized, decisions)
            selected, drop_priority_stages, penalty_encoding = _prepare_sla_hierarchy(
                ordered[:daily_limit], decisions, data
            )
            selected_penalties = [
                item.drop_penalty for item in selected if item.drop_penalty > 0
            ]
            solver_drop_divisor = (
                math.gcd(*selected_penalties) if selected_penalties else 1
            )
            for selected_job in selected:
                decisions[selected_job.id]["solver_drop_cost"] = (
                    selected_job.drop_penalty // solver_drop_divisor
                )
            selected_ids = {item.id for item in selected}
            solver_penalties = {item.id: item.drop_penalty for item in selected}
            deferred = [
                UnassignedJob(
                    job_id=item.id,
                    drop_penalty=item.drop_penalty,
                    reason_code=ReasonCode.DATASET_LIMIT,
                )
                for item in ordered[daily_limit:]
            ]
            data = replace(
                data,
                config=replace(
                    data.config,
                    max_jobs_per_run=daily_limit,
                    solver_time_limit_sec=min(
                        data.config.solver_time_limit_sec,
                        max(
                            1,
                            int(
                                config.batch_total_time_limit_sec
                                - (monotonic_time.monotonic() - started)
                            ),
                        ),
                    ),
                ),
                solve_deadline_monotonic=started + config.batch_total_time_limit_sec,
                jobs=selected,
                pre_unassigned=[
                    item
                    for item in data.pre_unassigned
                    if item.job_id not in selected_ids
                ]
                + deferred,
                snapshot={
                    **data.snapshot,
                    "drop_priority_stages": drop_priority_stages,
                    "penalty_encoding": penalty_encoding,
                    "jobs": [
                        {
                            **item,
                            "drop_penalty": solver_penalties.get(
                                item["id"],
                                decisions.get(item["id"], {}).get(
                                    "cascade_drop_penalty",
                                    item.get("drop_penalty", 0),
                                ),
                            ),
                        }
                        for item in data.snapshot["jobs"]
                    ],
                    "planning_batch_id": batch_id,
                },
            )
            await self._repository.mark_day_running(
                batch_id, current, sorted(remaining)
            )
            run_id: int | None = None
            try:
                run_id = await self._repository.create_daily_run(
                    batch_id,
                    current,
                    data.timezone,
                    batch["initiated_by_user_id"],
                )
                await self._repository.mark_daily_run_running(run_id, data)
                if data.jobs:
                    result = await self._solver_factory.create(
                        data.config.travel_provider
                    ).solve(data)
                else:
                    drop_cost = sum(item.drop_penalty for item in data.pre_unassigned)
                    result = PlanningResult(
                        routes=[],
                        unassigned=list(data.pre_unassigned),
                        solver_status="EMPTY",
                        objective=drop_cost,
                        drop_cost=drop_cost,
                        travel_cost=0,
                        solver_time_ms=0,
                    )
                result.validation_errors = self._daily_validator.validate(data, result)
                if result.validation_errors:
                    raise RuntimeError("; ".join(result.validation_errors))
                day_assigned = {
                    item.job_id for route in result.routes for item in route.jobs
                }
                prospective_dates = {
                    **assignment_dates,
                    **{job_id: current for job_id in day_assigned},
                }
                batch_validation_errors = self._batch_validator.validate_day(
                    data,
                    result,
                    remaining,
                    assigned_sequence,
                    effective_start,
                    maximum_end,
                    decisions,
                ) + self._batch_validator.validate(
                    [*assigned_sequence, *sorted(day_assigned)],
                    effective_start,
                    maximum_end,
                    prospective_dates,
                )
                if batch_validation_errors:
                    raise _BatchValidationError("; ".join(batch_validation_errors))
                assigned = await self._repository.save_day_result(
                    batch_id, run_id, data, result, decisions
                )
            except _BatchValidationError as error:
                await self._repository.fail_day(
                    batch_id,
                    current,
                    run_id,
                    "BATCH_VALIDATION_FAILED",
                    str(error),
                )
                await self._repository.fail_batch(
                    batch_id, "BATCH_VALIDATION_FAILED", str(error)
                )
                return
            except Exception as error:
                await self._repository.fail_day(
                    batch_id,
                    current,
                    run_id,
                    "DAY_RUN_FAILED",
                    str(error),
                )
                await self._finish(
                    batch_id,
                    "PARTIAL" if successful else "FAILED",
                    "DAY_RUN_FAILED",
                    remaining,
                    started,
                    processed,
                    successful,
                    skipped,
                    assigned_sequence,
                    existing_permanent + len(issues),
                )
                return
            for job_id in sorted(assigned):
                assigned_sequence.append(job_id)
                assignment_dates[job_id] = current
            remaining.difference_update(assigned)
            processed += 1
            successful += 1
            current += timedelta(days=1)

        if not remaining:
            permanent_count = existing_permanent + len(issues)
            await self._finish(
                batch_id,
                "SUCCESS" if permanent_count == 0 else "PARTIAL",
                (
                    "ALL_ELIGIBLE_ASSIGNED"
                    if permanent_count == 0
                    else "NO_FUTURE_OPPORTUNITIES"
                ),
                remaining,
                started,
                processed,
                successful,
                skipped,
                assigned_sequence,
                permanent_count,
            )
        else:
            await self._finish(
                batch_id,
                "PARTIAL",
                "HORIZON_LIMIT",
                remaining,
                started,
                processed,
                successful,
                skipped,
                assigned_sequence,
                existing_permanent + len(issues),
            )

    async def _finish(
        self,
        batch_id: int,
        status: str,
        reason: str,
        remaining: set[int],
        started: float,
        processed: int,
        successful: int,
        skipped: int,
        assigned: list[int],
        permanent: int,
    ) -> None:
        if status in {"SUCCESS", "PARTIAL"}:
            validation_errors = await self._repository.validate_terminal_state(
                batch_id, status, reason
            )
            if validation_errors:
                await self._repository.fail_batch(
                    batch_id,
                    "BATCH_VALIDATION_FAILED",
                    "; ".join(validation_errors),
                )
                return
        await self._repository.finish_batch(
            batch_id,
            status,
            reason,
            remaining,
            {
                "duration_ms": int((monotonic_time.monotonic() - started) * 1000),
                "processed_days": processed,
                "successful_days": successful,
                "skipped_days": skipped,
                "assigned": len(assigned),
                "remaining": len(remaining),
                "permanent_issues": permanent,
            },
        )


def _order_for_daily_limit(
    jobs: list,
    decisions: dict[int, dict[str, Any]],
) -> list:
    """Apply the deterministic primary-first dataset limit ordering."""
    return sorted(
        jobs,
        key=lambda item: (
            _group_order(decisions[item.id]["priority_group"]),
            work_priority_rank(item.priority),
            decisions[item.id].get("future_opportunity_count", 0),
            -item.drop_penalty,
            item.sla_date,
            item.created_at,
            item.id,
        ),
    )


def _enforce_sla_hierarchy(
    jobs: list,
    decisions: dict[int, dict[str, Any]],
    _data: PlanningInput,
) -> list:
    """Encode SLA, then work priority, then count and secondary priority.

    Mandatory nodes cannot be dropped and must not inflate optional penalties.
    Within a category, subtracting a common minimum preserves all comparisons
    at equal cardinality. Dividing differences by their GCD is also exact.
    This removes large constant SLA/priority bonuses without losing priority.
    """
    optional = [item for item in jobs if not item.mandatory]
    lower_priority_total = 0
    adjusted_by_id = {
        item.id: replace(item, drop_penalty=0) for item in jobs if item.mandatory
    }
    groups = sorted(
        {
            (
                decisions[item.id]["priority_group"],
                work_priority(item.priority).value,
            )
            for item in optional
        },
        key=lambda value: (_group_order(value[0]), work_priority_rank(value[1])),
        reverse=True,
    )
    for group in groups:
        group_jobs = [
            item
            for item in optional
            if (
                decisions[item.id]["priority_group"],
                work_priority(item.priority).value,
            )
            == group
        ]
        minimum = min(item.drop_penalty for item in group_jobs)
        differences = [item.drop_penalty - minimum for item in group_jobs]
        divisor = math.gcd(*differences) or 1
        secondary = [value // divisor for value in differences]
        secondary_weight = lower_priority_total + 1
        floor = (sum(secondary) + 1) * secondary_weight
        for item, value in zip(group_jobs, secondary, strict=True):
            penalty = floor + value * secondary_weight
            if penalty >= 2**62:
                raise RuntimeError("INVALID_PENALTY_BANDS")
            adjusted_by_id[item.id] = replace(item, drop_penalty=penalty)
        lower_priority_total += sum(
            adjusted_by_id[item.id].drop_penalty for item in group_jobs
        )
        if lower_priority_total >= 2**62:
            raise RuntimeError("INVALID_PENALTY_BANDS")
    return [adjusted_by_id[item.id] for item in jobs]


def _prepare_sla_hierarchy(
    jobs: list,
    decisions: dict[int, dict[str, Any]],
    data: PlanningInput,
) -> tuple[list, list[dict[str, Any]], str]:
    """Подготовить прежние штрафы и независимые уровни на случай переполнения.

    Каждый уровень кодирует две прежние цели одной SLA-категории: сначала число
    пропусков, затем сумму вторичных баллов. Между категориями большие веса не
    нужны: при переполнении solver рассматривает уровни последовательно.
    """
    optional = [item for item in jobs if not item.mandatory]
    groups = sorted(
        {
            (
                decisions[item.id]["priority_group"],
                work_priority(item.priority).value,
            )
            for item in optional
        },
        key=lambda value: (_group_order(value[0]), work_priority_rank(value[1])),
    )
    stages: list[dict[str, Any]] = []
    for priority_group_name, priority in groups:
        group_jobs = [
            item
            for item in optional
            if (
                decisions[item.id]["priority_group"],
                work_priority(item.priority).value,
            )
            == (priority_group_name, priority)
        ]
        minimum = min(item.drop_penalty for item in group_jobs)
        differences = [item.drop_penalty - minimum for item in group_jobs]
        divisor = math.gcd(*differences) or 1
        secondary = [value // divisor for value in differences]
        count_weight = sum(secondary) + 1
        costs = {
            str(item.id): count_weight + value
            for item, value in zip(group_jobs, secondary, strict=True)
        }
        maximum = sum(costs.values())
        if maximum >= 2**63 - 1:
            raise RuntimeError("OBJECTIVE_RANGE_OVERFLOW")
        stages.append(
            {
                "name": f"{priority_group_name}:{priority}",
                "priority_group": priority_group_name,
                "priority": priority,
                "job_costs": costs,
                "count_weight": count_weight,
                "secondary_divisor": divisor,
                "maximum_objective": maximum,
            }
        )

    try:
        adjusted = _enforce_sla_hierarchy(jobs, decisions, data)
    except RuntimeError as error:
        if str(error) != "INVALID_PENALTY_BANDS":
            raise
        # Исходные CascadeDropPenalty остаются в отчёте и помещаются в BigInteger;
        # solver использует небольшие job_costs отдельных уровней.
        return jobs, stages, "LEXICOGRAPHIC_STAGES_REQUIRED"
    return adjusted, stages, "WEIGHTED_OR_LEXICOGRAPHIC"


def _group_order(group: str) -> int:
    return {
        "OVERDUE": 0,
        "DUE_TODAY": 1,
        "DUE_IN_1_DAY": 2,
        "DUE_IN_2_3_DAYS": 3,
        "DUE_LATER_IN_CURRENT_BLOCK": 4,
        "RESERVE": 5,
    }[group]


class _BatchValidationError(RuntimeError):
    pass


def _daily_source(
    snapshot: dict[str, Any],
    planning_date: date,
    remaining: set[int],
    schedules: list[dict[str, Any]],
) -> dict[str, Any]:
    engineers = {int(item["id"]): item for item in snapshot["engineers"]}
    engineer_rows = []
    for schedule in schedules:
        engineer = engineers[int(schedule["engineer_id"])]
        engineer_rows.append(
            {
                "engineer_id": engineer["id"],
                "transport_type": engineer["transport_type"],
                "start_address": engineer["start_address"],
                "start_latitude": engineer["start_latitude"],
                "start_longitude": engineer["start_longitude"],
                "shift_start": _time(schedule["shift_start"]),
                "shift_end": _time(schedule["shift_end"]),
            }
        )
    jobs = []
    for item in snapshot["jobs"]:
        if int(item["id"]) not in remaining:
            continue
        value = dict(item)
        value["sla_date"] = _date(value["sla_date"])
        value["created_at"] = datetime.fromisoformat(
            value["created_at"].replace("Z", "+00:00")
        )
        value["time_window_start"] = _optional_time(value["time_window_start"])
        value["time_window_end"] = _optional_time(value["time_window_end"])
        jobs.append(value)
    return {
        "project": snapshot["project"],
        "config": dict(snapshot["config"]),
        "jobs": jobs,
        "engineers": engineer_rows,
        "required_qualifications": {
            int(key): set(value)
            for key, value in snapshot["required_qualifications"].items()
        },
        "required_equipment": {
            int(key): set(value)
            for key, value in snapshot["required_equipment"].items()
        },
        "engineer_qualifications": {
            int(key): set(value)
            for key, value in snapshot["engineer_qualifications"].items()
        },
        "equipment_units": {
            int(key): int(value) for key, value in snapshot["equipment_units"].items()
        },
    }


def _schedules_for(
    snapshot: dict[str, Any], planning_date: date
) -> list[dict[str, Any]]:
    return [
        item
        for item in snapshot["schedules"]
        if _date(item["work_date"]) == planning_date
    ]


def _date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _time(value: time | str) -> time:
    return value if isinstance(value, time) else time.fromisoformat(value)


def _optional_time(value: time | str | None) -> time | None:
    return None if value is None else _time(value)


def _datetime(value: datetime | str | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
