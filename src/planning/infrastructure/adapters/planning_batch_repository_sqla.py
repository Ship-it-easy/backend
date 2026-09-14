import hashlib
import json
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any

from sqlalchemy import and_, bindparam, exists, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.errors import (
    ConflictError,
    InvalidPlanningRequest,
    ObjectNotFoundError,
    PlanningRunInProgress,
)
from planning.domain.entities.planning import (
    PlanningConfig,
    PlanningInput,
    PlanningResult,
)
from planning.domain.enums import ACTIVE_BATCH_STATUSES
from planning.infrastructure.adapters.planning_run_repository_sqla import (
    SqlaPlanningRunRepository,
)
from planning.infrastructure.persistence_sqla.mappings.tables import (
    assignments,
    daily_plans,
    engineer_qualifications,
    engineer_schedules,
    engineers,
    equipment_types,
    jobs,
    plan_versions,
    planning_batch_days,
    planning_batch_jobs,
    planning_batches,
    planning_config,
    planning_route_jobs,
    planning_runs,
    projects,
    work_type_required_equipment,
    work_type_required_qualifications,
    work_types,
)


class SqlaPlanningBatchRepository:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._daily = SqlaPlanningRunRepository(session)

    async def get_project_timezone(self, project_id: int) -> str:
        return await self._daily.get_project_timezone(project_id)

    async def create_or_reuse(
        self,
        project_id: int,
        requested_start_date: date,
        initiated_by_user_id: Any,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        project = (
            (
                await self._session.execute(
                    select(projects)
                    .where(projects.c.id == project_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if project is None:
            raise ObjectNotFoundError("Project not found")
        if not project.planning_one_day_enabled:
            raise ConflictError(
                "Planning is not enabled for this project",
                code="PLANNING_CONFIGURATION_INVALID",
            )
        existing_key = (
            (
                await self._session.execute(
                    select(planning_batches).where(
                        planning_batches.c.project_id == project_id,
                        planning_batches.c.idempotency_key == idempotency_key,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing_key is not None:
            if existing_key.requested_start_date != requested_start_date:
                raise ConflictError(
                    "Idempotency-Key was already used for another planning date",
                    code="IDEMPOTENCY_KEY_REUSED",
                )
            return _jsonable(dict(existing_key)), True

        active = (
            (
                await self._session.execute(
                    select(planning_batches).where(
                        planning_batches.c.project_id == project_id,
                        planning_batches.c.status.in_(ACTIVE_BATCH_STATUSES),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if active is not None:
            raise ConflictError(
                "A planning batch is already active for this project",
                code="PLANNING_BATCH_IN_PROGRESS",
            )
        config = await self._active_config(project_id)
        if config is None:
            raise ConflictError(
                "Project has no active planning configuration",
                code="PLANNING_CONFIGURATION_INVALID",
            )
        if (
            int(config.batch_initial_horizon_days) != 7
            or int(config.batch_maximum_horizon_days) != 30
            or not 1 <= int(config.batch_total_time_limit_sec) <= 900
            or not 1 <= int(config.solver_time_limit_sec) <= 600
            or not 1 <= int(config.max_jobs_per_run) <= 1000
            or not 1 <= int(config.max_jobs_per_batch) <= 5000
            or int(config.travel_cost_per_minute) < 0
            or min(
                int(config.future_opportunity_critical),
                int(config.future_opportunity_high),
                int(config.future_opportunity_limited),
            )
            < 0
            or not (
                int(config.future_opportunity_critical)
                >= int(config.future_opportunity_high)
                >= int(config.future_opportunity_limited)
            )
        ):
            raise ConflictError(
                "Multi-day planning configuration is invalid",
                code="PLANNING_CONFIGURATION_INVALID",
            )
        effective_start = await self._effective_start(project_id, requested_start_date)
        maximum_end = effective_start + timedelta(
            days=int(config.batch_maximum_horizon_days) - 1
        )
        snapshot = await self._build_snapshot(
            project_id, effective_start, maximum_end, project, config
        )
        max_jobs = int(config.max_jobs_per_batch)
        if len(snapshot["jobs"]) > max_jobs:
            raise InvalidPlanningRequest(
                f"Eligible batch size exceeds configured limit ({max_jobs})",
                code="BATCH_DATASET_LIMIT",
            )
        input_hash = _snapshot_hash(snapshot)
        current = (
            (
                await self._session.execute(
                    select(planning_batches).where(
                        planning_batches.c.project_id == project_id,
                        planning_batches.c.current_flag.is_(True),
                        planning_batches.c.input_hash == input_hash,
                        planning_batches.c.configuration_version == str(config.version),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if current is not None:
            return _jsonable(dict(current)), True

        initial_end = min(
            maximum_end,
            effective_start
            + timedelta(days=int(config.batch_initial_horizon_days) - 1),
        )
        try:
            row = (
                (
                    await self._session.execute(
                        insert(planning_batches)
                        .values(
                            project_id=project_id,
                            requested_start_date=requested_start_date,
                            effective_start_date=effective_start,
                            initial_horizon_end=initial_end,
                            maximum_horizon_end=maximum_end,
                            status="CREATED",
                            initiated_by_user_id=initiated_by_user_id,
                            idempotency_key=idempotency_key,
                            input_hash=input_hash,
                            configuration_version=str(config.version),
                            input_snapshot=snapshot,
                        )
                        .returning(*planning_batches.c)
                    )
                )
                .mappings()
                .one()
            )
            if snapshot["jobs"]:
                await self._session.execute(
                    insert(planning_batch_jobs),
                    [
                        {
                            "planning_batch_id": row.id,
                            "job_id": item["id"],
                            "snapshot_sla_date": _as_date(item["sla_date"]),
                            "eligibility_status": "ELIGIBLE",
                            "processing_status": "BACKLOG",
                        }
                        for item in snapshot["jobs"]
                    ],
                )
            if effective_start > requested_start_date:
                await self._session.execute(
                    insert(planning_batch_days).values(
                        planning_batch_id=row.id,
                        planning_date=requested_start_date,
                        block_number=0,
                        status="PINNED_PUBLISHED",
                        finished_at=datetime.now(timezone.utc),
                    )
                )
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise ConflictError(
                "A planning batch is already active for this project",
                code="PLANNING_BATCH_IN_PROGRESS",
            ) from error
        return _jsonable(dict(row)), False

    async def load_for_execution(self, batch_id: int) -> dict[str, Any] | None:
        row = (
            (
                await self._session.execute(
                    select(planning_batches).where(planning_batches.c.id == batch_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        job_states = (
            (
                await self._session.execute(
                    select(
                        planning_batch_jobs.c.job_id,
                        planning_batch_jobs.c.processing_status,
                        planning_batch_jobs.c.assigned_date,
                    ).where(planning_batch_jobs.c.planning_batch_id == batch_id)
                )
            )
            .mappings()
            .all()
        )
        day_states = (
            (
                await self._session.execute(
                    select(planning_batch_days).where(
                        planning_batch_days.c.planning_batch_id == batch_id
                    )
                )
            )
            .mappings()
            .all()
        )
        value = dict(row)
        value["execution_job_states"] = [dict(item) for item in job_states]
        value["execution_day_states"] = [dict(item) for item in day_states]
        return _jsonable(value)

    async def mark_batch_status(self, batch_id: int, status: str) -> None:
        values: dict[str, Any] = {"status": status}
        if status in {"PREPARING", "RUNNING"}:
            values["started_at"] = func.coalesce(
                planning_batches.c.started_at, datetime.now(timezone.utc)
            )
        await self._session.execute(
            update(planning_batches)
            .where(planning_batches.c.id == batch_id)
            .values(**values)
        )
        await self._session.commit()

    async def should_stop(self, batch_id: int) -> bool:
        value = await self._session.scalar(
            select(planning_batches.c.status).where(planning_batches.c.id == batch_id)
        )
        return value == "STOP_REQUESTED"

    async def create_days(self, batch_id: int, dates: list[date]) -> None:
        if not dates:
            return
        batch = (
            await self._session.execute(
                select(planning_batches.c.effective_start_date).where(
                    planning_batches.c.id == batch_id
                )
            )
        ).one()
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        await self._session.execute(
            pg_insert(planning_batch_days)
            .values(
                [
                    {
                        "planning_batch_id": batch_id,
                        "planning_date": item,
                        "block_number": ((item - batch.effective_start_date).days // 7)
                        + 1,
                        "status": "PENDING",
                    }
                    for item in dates
                ]
            )
            .on_conflict_do_nothing(
                index_elements=["planning_batch_id", "planning_date"]
            )
        )
        await self._session.commit()

    async def mark_day_running(
        self, batch_id: int, planning_date: date, input_job_ids: list[int]
    ) -> None:
        digest = hashlib.sha256(
            json.dumps(sorted(input_job_ids), separators=(",", ":")).encode()
        ).hexdigest()
        await self._session.execute(
            update(planning_batch_days)
            .where(
                planning_batch_days.c.planning_batch_id == batch_id,
                planning_batch_days.c.planning_date == planning_date,
            )
            .values(
                status="RUNNING",
                input_jobs_count=len(input_job_ids),
                input_job_ids_hash=digest,
                started_at=datetime.now(timezone.utc),
            )
        )
        await self._session.commit()

    async def create_daily_run(
        self, batch_id: int, planning_date: date, timezone_name: str, user_id: Any
    ) -> int:
        try:
            run_id = await self._session.scalar(
                insert(planning_runs)
                .values(
                    project_id=select(planning_batches.c.project_id)
                    .where(planning_batches.c.id == batch_id)
                    .scalar_subquery(),
                    planning_batch_id=batch_id,
                    planning_date=planning_date,
                    timezone=timezone_name,
                    status="PREPARING",
                    started_at=datetime.now(timezone.utc),
                    initiated_by_user_id=user_id,
                )
                .returning(planning_runs.c.id)
            )
            await self._session.commit()
            return int(run_id)
        except IntegrityError as error:
            await self._session.rollback()
            raise PlanningRunInProgress(
                "Planning date is locked by another run",
                code="PLANNING_DATE_LOCKED_BY_BATCH",
            ) from error

    async def mark_daily_run_running(self, run_id: int, data: PlanningInput) -> None:
        await self._daily.mark_running(run_id, data)

    async def save_day_result(
        self,
        batch_id: int,
        run_id: int,
        data: PlanningInput,
        result: PlanningResult,
        decisions: dict[int, dict[str, Any]],
    ) -> set[int]:
        # Daily routes, assignments, diagnostics and the batch-day transition are
        # committed together. A failure below rolls the complete day back.
        await self._daily.save_result(run_id, data, result, commit=False)
        assigned_ids = {item.job_id for route in result.routes for item in route.jobs}
        candidate_ids = {item.id for item in data.jobs}
        if decisions:
            await self._session.execute(
                update(planning_batch_jobs)
                .where(
                    planning_batch_jobs.c.planning_batch_id
                    == bindparam("batch_key"),
                    planning_batch_jobs.c.job_id == bindparam("job_key"),
                )
                .values(
                    priority_group=bindparam("priority_group"),
                    future_opportunity_count=bindparam("future_opportunity_count"),
                    future_opportunity_rank=bindparam("future_opportunity_rank"),
                    future_opportunity_bonus=bindparam("future_opportunity_bonus"),
                    daily_drop_penalty=bindparam("daily_drop_penalty"),
                    cascade_drop_penalty=bindparam("cascade_drop_penalty"),
                ),
                [
                    {"batch_key": batch_id, "job_key": job_id, **values}
                    for job_id, values in decisions.items()
                ],
            )
        if candidate_ids:
            await self._session.execute(
                update(planning_batch_jobs)
                .where(
                    planning_batch_jobs.c.planning_batch_id == batch_id,
                    planning_batch_jobs.c.job_id.in_(candidate_ids),
                )
                .values(
                    last_considered_date=data.planning_date,
                    last_planning_run_id=run_id,
                    attempt_count=planning_batch_jobs.c.attempt_count + 1,
                )
            )
        if assigned_ids:
            await self._session.execute(
                update(planning_batch_jobs)
                .where(
                    planning_batch_jobs.c.planning_batch_id == batch_id,
                    planning_batch_jobs.c.job_id.in_(assigned_ids),
                )
                .values(
                    processing_status="DRAFT_ASSIGNED",
                    assigned_date=data.planning_date,
                    planning_run_id=run_id,
                    primary_reason_code=None,
                )
            )
        unassigned_updates = [
            {
                "batch_key": batch_id,
                "job_key": item.job_id,
                "reason": _cascade_daily_reason(item.reason_code.value),
                "flags": item.diagnostic_flags,
            }
            for item in result.unassigned
            if item.job_id not in assigned_ids
        ]
        if unassigned_updates:
            await self._session.execute(
                update(planning_batch_jobs)
                .where(
                    planning_batch_jobs.c.planning_batch_id
                    == bindparam("batch_key"),
                    planning_batch_jobs.c.job_id == bindparam("job_key"),
                )
                .values(
                    primary_reason_code=bindparam("reason"),
                    diagnostic_flags=bindparam("flags"),
                ),
                unassigned_updates,
            )
        await self._session.execute(
            update(planning_batch_days)
            .where(
                planning_batch_days.c.planning_batch_id == batch_id,
                planning_batch_days.c.planning_date == data.planning_date,
            )
            .values(
                status="SUCCESS",
                planning_run_id=run_id,
                solver_candidates_count=len(data.jobs),
                assigned_count=len(assigned_ids),
                unassigned_count=len(result.unassigned),
                deferred_count=len(result.unassigned),
                dropped_count=sum(
                    1 for item in result.unassigned if item.job_id in candidate_ids
                ),
                finished_at=datetime.now(timezone.utc),
            )
        )
        await self._session.execute(
            update(planning_batches)
            .where(planning_batches.c.id == batch_id)
            .values(processed_through_date=data.planning_date, status="RUNNING")
        )
        await self._session.commit()
        return assigned_ids

    async def skip_day(
        self, batch_id: int, planning_date: date, remaining_count: int
    ) -> None:
        await self._session.execute(
            update(planning_batch_days)
            .where(
                planning_batch_days.c.planning_batch_id == batch_id,
                planning_batch_days.c.planning_date == planning_date,
            )
            .values(
                status="SKIPPED_NO_SHIFT",
                input_jobs_count=remaining_count,
                deferred_count=remaining_count,
                finished_at=datetime.now(timezone.utc),
            )
        )
        await self._session.execute(
            update(planning_batches)
            .where(planning_batches.c.id == batch_id)
            .values(processed_through_date=planning_date, status="RUNNING")
        )
        await self._session.commit()

    async def fail_day(
        self,
        batch_id: int,
        planning_date: date,
        run_id: int | None,
        code: str,
        message: str,
    ) -> None:
        if run_id is not None:
            await self._daily.fail_run(run_id, code, message)
        await self._session.execute(
            update(planning_batch_days)
            .where(
                planning_batch_days.c.planning_batch_id == batch_id,
                planning_batch_days.c.planning_date == planning_date,
            )
            .values(
                status="FAILED",
                planning_run_id=run_id,
                error_code=code,
                finished_at=datetime.now(timezone.utc),
            )
        )
        await self._session.commit()

    async def set_permanent_issues(
        self, batch_id: int, issues: dict[int, tuple[str, dict[str, Any]]]
    ) -> None:
        if issues:
            await self._session.execute(
                update(planning_batch_jobs)
                .where(
                    planning_batch_jobs.c.planning_batch_id
                    == bindparam("batch_key"),
                    planning_batch_jobs.c.job_id == bindparam("job_key"),
                )
                .values(
                    eligibility_status="PERMANENT_ISSUE",
                    processing_status="PERMANENT_ISSUE",
                    primary_reason_code=bindparam("reason"),
                    diagnostic_flags=bindparam("flags"),
                    future_opportunity_count=0,
                    future_opportunity_rank=0,
                    future_opportunity_bonus=0,
                ),
                [
                    {
                        "batch_key": batch_id,
                        "job_key": job_id,
                        "reason": reason,
                        "flags": flags,
                    }
                    for job_id, (reason, flags) in issues.items()
                ],
            )
        await self._session.commit()

    async def validate_terminal_state(
        self, batch_id: int, status: str, completion_reason: str
    ) -> list[str]:
        batch = (
            await self._session.execute(
                select(
                    planning_batches.c.project_id,
                    planning_batches.c.effective_start_date,
                    planning_batches.c.maximum_horizon_end,
                ).where(planning_batches.c.id == batch_id)
            )
        ).one()
        job_states = (
            (
                await self._session.execute(
                    select(
                        planning_batch_jobs.c.job_id,
                        planning_batch_jobs.c.processing_status,
                        planning_batch_jobs.c.assigned_date,
                    ).where(planning_batch_jobs.c.planning_batch_id == batch_id)
                )
            )
            .mappings()
            .all()
        )
        route_assignments = (
            (
                await self._session.execute(
                    select(
                        planning_route_jobs.c.job_id,
                        planning_runs.c.planning_date,
                        planning_runs.c.status,
                        planning_runs.c.validation_errors,
                    )
                    .join(
                        planning_runs,
                        planning_runs.c.id == planning_route_jobs.c.planning_run_id,
                    )
                    .where(planning_runs.c.planning_batch_id == batch_id)
                )
            )
            .mappings()
            .all()
        )
        day_states = (
            (
                await self._session.execute(
                    select(planning_batch_days).where(
                        planning_batch_days.c.planning_batch_id == batch_id
                    )
                )
            )
            .mappings()
            .all()
        )
        run_states = {
            int(item.id): item
            for item in (
                (
                    await self._session.execute(
                        select(
                            planning_runs.c.id,
                            planning_runs.c.project_id,
                            planning_runs.c.planning_date,
                            planning_runs.c.status,
                            planning_runs.c.validation_errors,
                        ).where(planning_runs.c.planning_batch_id == batch_id)
                    )
                )
                .mappings()
                .all()
            )
        }
        errors: list[str] = []
        assignment_counts = Counter(int(item.job_id) for item in route_assignments)
        duplicate_ids = sorted(
            job_id for job_id, count in assignment_counts.items() if count > 1
        )
        if duplicate_ids:
            errors.append(f"jobs assigned more than once: {duplicate_ids}")
        states_by_id = {int(item.job_id): item for item in job_states}
        for item in route_assignments:
            job_id = int(item.job_id)
            state = states_by_id.get(job_id)
            if state is None or state.processing_status != "DRAFT_ASSIGNED":
                errors.append(f"job {job_id} route and batch state disagree")
                continue
            if state.assigned_date != item.planning_date:
                errors.append(f"job {job_id} assigned date disagrees with route")
            if not (
                batch.effective_start_date
                <= item.planning_date
                <= batch.maximum_horizon_end
            ):
                errors.append(f"job {job_id} is assigned outside batch horizon")
            if item.status != "SUCCESS" or item.validation_errors:
                errors.append(f"job {job_id} belongs to an invalid daily run")
        for item in job_states:
            route_count = assignment_counts.get(int(item.job_id), 0)
            if item.processing_status == "DRAFT_ASSIGNED" and route_count != 1:
                errors.append(f"job {item.job_id} draft assignment is missing")
            if item.processing_status != "DRAFT_ASSIGNED" and route_count:
                errors.append(f"job {item.job_id} has conflicting terminal states")
        for day in day_states:
            if day.status == "PINNED_PUBLISHED":
                continue
            if not (
                batch.effective_start_date
                <= day.planning_date
                <= batch.maximum_horizon_end
            ):
                errors.append(f"day {day.planning_date} is outside batch horizon")
            expected_block = (
                (day.planning_date - batch.effective_start_date).days // 7
            ) + 1
            if day.block_number != expected_block:
                errors.append(f"day {day.planning_date} has an invalid block number")
            if day.status == "SUCCESS":
                run = run_states.get(int(day.planning_run_id or 0))
                if (
                    run is None
                    or run.project_id != batch.project_id
                    or run.planning_date != day.planning_date
                    or run.status != "SUCCESS"
                    or run.validation_errors
                ):
                    errors.append(f"day {day.planning_date} has no valid daily run")
        if status == "SUCCESS" and any(
            item.processing_status != "DRAFT_ASSIGNED" for item in job_states
        ):
            errors.append("SUCCESS batch still contains unassigned eligible jobs")
        if status == "PARTIAL" and not completion_reason:
            errors.append("PARTIAL batch has no completion reason")
        foreign_jobs = await self._session.scalar(
            select(jobs.c.id)
            .join(planning_batch_jobs, planning_batch_jobs.c.job_id == jobs.c.id)
            .where(
                planning_batch_jobs.c.planning_batch_id == batch_id,
                jobs.c.project_id != batch.project_id,
            )
            .limit(1)
        )
        if foreign_jobs is not None:
            errors.append("batch contains a job from another project")
        return errors

    async def finish_batch(
        self,
        batch_id: int,
        status: str,
        completion_reason: str,
        remaining_job_ids: set[int],
        metrics: dict[str, Any],
    ) -> None:
        if remaining_job_ids and completion_reason in {
            "HORIZON_LIMIT",
            "TOTAL_TIME_LIMIT",
        }:
            await self._session.execute(
                update(planning_batch_jobs)
                .where(
                    planning_batch_jobs.c.planning_batch_id == batch_id,
                    planning_batch_jobs.c.job_id.in_(remaining_job_ids),
                    planning_batch_jobs.c.processing_status == "BACKLOG",
                )
                .values(
                    processing_status="UNASSIGNED_WITHIN_HORIZON",
                    primary_reason_code="NOT_ASSIGNED_WITHIN_HORIZON",
                )
            )
        if status in {"SUCCESS", "PARTIAL"}:
            project_id = await self._session.scalar(
                select(planning_batches.c.project_id).where(
                    planning_batches.c.id == batch_id
                )
            )
            await self._session.execute(
                update(planning_batches)
                .where(
                    planning_batches.c.project_id == project_id,
                    planning_batches.c.current_flag.is_(True),
                    planning_batches.c.id != batch_id,
                )
                .values(current_flag=False, status="SUPERSEDED")
            )
        await self._session.execute(
            update(planning_batches)
            .where(planning_batches.c.id == batch_id)
            .values(
                status=status,
                completion_reason=completion_reason,
                current_flag=status in {"SUCCESS", "PARTIAL"},
                metrics=metrics,
                finished_at=datetime.now(timezone.utc),
            )
        )
        await self._session.commit()

    async def fail_batch(self, batch_id: int, code: str, message: str) -> None:
        await self._session.rollback()
        successful_days = int(
            await self._session.scalar(
                select(planning_batch_days.c.id)
                .where(
                    planning_batch_days.c.planning_batch_id == batch_id,
                    planning_batch_days.c.status == "SUCCESS",
                )
                .limit(1)
            )
            is not None
        )
        validation_failed = code == "BATCH_VALIDATION_FAILED"
        await self._session.execute(
            update(planning_batches)
            .where(planning_batches.c.id == batch_id)
            .values(
                status=(
                    "FAILED"
                    if validation_failed or not successful_days
                    else "PARTIAL"
                ),
                completion_reason=(
                    "SYSTEM_ERROR"
                    if validation_failed or not successful_days
                    else "DAY_RUN_FAILED"
                ),
                error_code=code,
                error_message=message[:1000],
                finished_at=datetime.now(timezone.utc),
            )
        )
        await self._session.commit()

    async def get_batch(self, project_id: int, batch_id: int) -> dict[str, Any]:
        batch = (
            (
                await self._session.execute(
                    select(planning_batches).where(
                        planning_batches.c.id == batch_id,
                        planning_batches.c.project_id == project_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if batch is None:
            raise ObjectNotFoundError(
                "Planning batch not found", code="PLANNING_BATCH_NOT_FOUND"
            )
        days = (
            (
                await self._session.execute(
                    select(planning_batch_days)
                    .where(planning_batch_days.c.planning_batch_id == batch_id)
                    .order_by(planning_batch_days.c.planning_date)
                )
            )
            .mappings()
            .all()
        )
        job_rows = (
            (
                await self._session.execute(
                    select(
                        planning_batch_jobs,
                        jobs.c.address,
                    )
                    .join(jobs, jobs.c.id == planning_batch_jobs.c.job_id)
                    .where(planning_batch_jobs.c.planning_batch_id == batch_id)
                    .order_by(
                        planning_batch_jobs.c.snapshot_sla_date,
                        planning_batch_jobs.c.job_id,
                    )
                )
            )
            .mappings()
            .all()
        )
        engineer_names = {
            int(row.id): str(row.name)
            for row in (
                (
                    await self._session.execute(
                        select(engineers.c.id, engineers.c.name).where(
                            engineers.c.project_id == project_id
                        )
                    )
                )
                .mappings()
                .all()
            )
        }
        job_addresses = {int(row.job_id): str(row.address) for row in job_rows}
        day_values = []
        for day in days:
            value = dict(day)
            if day.planning_run_id is not None and day.status == "SUCCESS":
                run = await self._daily.get_run(project_id, int(day.planning_run_id))
                value["routes"] = run["routes"]
                value["unassigned_jobs"] = run["unassigned_jobs"]
                for route in value["routes"]:
                    route["engineer_name"] = engineer_names.get(
                        int(route["engineer_id"]), "Инженер"
                    )
                    for job in route["jobs"]:
                        job["address"] = job_addresses.get(
                            int(job["job_id"]), "Адрес не указан"
                        )
            else:
                value["routes"] = []
                value["unassigned_jobs"] = []
            day_values.append(value)
        backlog = [
            dict(item)
            for item in job_rows
            if item.processing_status != "DRAFT_ASSIGNED"
        ]
        assigned = sum(
            1 for item in job_rows if item.processing_status == "DRAFT_ASSIGNED"
        )
        processed = sum(
            1 for item in days if item.status in {"SUCCESS", "SKIPPED_NO_SHIFT"}
        )
        payload = dict(batch)
        payload.pop("input_snapshot", None)
        payload.update(
            days=day_values,
            backlog=backlog,
            progress={
                "processed_days": processed,
                "opened_days": len(days),
                "assigned": assigned,
                "remaining": len(backlog),
            },
        )
        return _jsonable(payload)

    async def list_batches(
        self,
        project_id: int,
        status: str | None,
        date_from: date | None,
        date_to: date | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        query = select(planning_batches).where(
            planning_batches.c.project_id == project_id
        )
        if status:
            query = query.where(planning_batches.c.status == status)
        if date_from:
            query = query.where(planning_batches.c.requested_start_date >= date_from)
        if date_to:
            query = query.where(planning_batches.c.requested_start_date <= date_to)
        rows = (
            (
                await self._session.execute(
                    query.order_by(planning_batches.c.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .mappings()
            .all()
        )
        values = []
        for row in rows:
            value = dict(row)
            value.pop("input_snapshot", None)
            values.append(value)
        return _jsonable(values)

    async def request_stop(self, project_id: int, batch_id: int) -> dict[str, Any]:
        row = (
            (
                await self._session.execute(
                    select(planning_batches)
                    .where(
                        planning_batches.c.id == batch_id,
                        planning_batches.c.project_id == project_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ObjectNotFoundError(
                "Planning batch not found", code="PLANNING_BATCH_NOT_FOUND"
            )
        if row.status in ACTIVE_BATCH_STATUSES and row.status != "STOP_REQUESTED":
            await self._session.execute(
                update(planning_batches)
                .where(planning_batches.c.id == batch_id)
                .values(
                    status="STOP_REQUESTED",
                    stop_requested_at=datetime.now(timezone.utc),
                )
            )
            await self._session.commit()
        return await self.get_batch(project_id, batch_id)

    async def validate_current_day(
        self, project_id: int, batch_id: int, *, commit: bool = True
    ) -> dict[str, Any]:
        batch = (
            (
                await self._session.execute(
                    select(planning_batches).where(
                        planning_batches.c.id == batch_id,
                        planning_batches.c.project_id == project_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if batch is None:
            raise ObjectNotFoundError(
                "Planning batch not found", code="PLANNING_BATCH_NOT_FOUND"
            )
        project = (
            (
                await self._session.execute(
                    select(projects).where(projects.c.id == project_id)
                )
            )
            .mappings()
            .one()
        )
        config = await self._active_config(project_id)
        if config is None:
            await self._session.execute(
                update(planning_batches)
                .where(planning_batches.c.id == batch_id)
                .values(stale_for_publication=True)
            )
            if commit:
                await self._session.commit()
            return {
                "planning_batch_id": batch_id,
                "valid_for_publication": False,
                "changed_entities_count": 1,
                "changed_categories": ["config"],
            }
        current = await self._build_snapshot(
            project_id,
            batch.effective_start_date,
            batch.maximum_horizon_end,
            project,
            config,
        )
        previous = batch.input_snapshot
        changed = [
            key
            for key in (
                "project",
                "jobs",
                "engineers",
                "schedules",
                "required_qualifications",
                "required_equipment",
                "engineer_qualifications",
                "equipment_units",
                "config",
            )
            if previous.get(key) != current.get(key)
        ]
        valid = not changed and _snapshot_hash(current) == batch.input_hash
        await self._session.execute(
            update(planning_batches)
            .where(planning_batches.c.id == batch_id)
            .values(stale_for_publication=not valid)
        )
        if commit:
            await self._session.commit()
        return {
            "planning_batch_id": batch_id,
            "valid_for_publication": valid,
            "changed_entities_count": len(changed),
            "changed_categories": changed,
        }

    async def _active_config(self, project_id: int):
        return (
            (
                await self._session.execute(
                    select(planning_config).where(
                        planning_config.c.project_id == project_id,
                        planning_config.c.active.is_(True),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )

    async def _effective_start(self, project_id: int, requested: date) -> date:
        published = await self._session.scalar(
            select(daily_plans.c.id).where(
                daily_plans.c.project_id == project_id,
                daily_plans.c.planning_date == requested,
                daily_plans.c.current_version_id.is_not(None),
            )
        )
        return requested + timedelta(days=1) if published is not None else requested

    async def _build_snapshot(
        self,
        project_id: int,
        effective_start: date,
        maximum_end: date,
        project: Any,
        config: Any,
    ) -> dict[str, Any]:
        active_assignment = exists(
            select(assignments.c.id)
            .select_from(
                assignments.join(
                    plan_versions,
                    assignments.c.plan_version_id == plan_versions.c.id,
                ).join(
                    daily_plans,
                    and_(
                        daily_plans.c.id == plan_versions.c.daily_plan_id,
                        daily_plans.c.current_version_id == plan_versions.c.id,
                    ),
                )
            )
            .where(
                assignments.c.job_id == jobs.c.id,
                assignments.c.active.is_(True),
            )
        )
        job_rows = (
            (
                await self._session.execute(
                    select(
                        jobs.c.id,
                        jobs.c.address,
                        jobs.c.latitude,
                        jobs.c.longitude,
                        jobs.c.sla_date,
                        jobs.c.time_window_start,
                        jobs.c.time_window_end,
                        jobs.c.work_type_id,
                        jobs.c.service_duration_min,
                        jobs.c.created_at,
                        jobs.c.updated_at,
                        work_types.c.default_service_duration_min,
                        work_types.c.required_transport,
                    )
                    .join(work_types, jobs.c.work_type_id == work_types.c.id)
                    .where(
                        jobs.c.project_id == project_id,
                        work_types.c.project_id == project_id,
                        jobs.c.status == "NEW",
                        jobs.c.sla_date <= maximum_end,
                        ~active_assignment,
                    )
                    .order_by(jobs.c.sla_date, jobs.c.created_at, jobs.c.id)
                )
            )
            .mappings()
            .all()
        )
        engineer_rows = (
            (
                await self._session.execute(
                    select(
                        engineers.c.id,
                        engineers.c.transport_type,
                        engineers.c.start_address,
                        engineers.c.start_latitude,
                        engineers.c.start_longitude,
                        engineers.c.updated_at,
                    )
                    .where(
                        engineers.c.project_id == project_id,
                        engineers.c.active.is_(True),
                    )
                    .order_by(engineers.c.id)
                )
            )
            .mappings()
            .all()
        )
        engineer_ids = {int(row.id) for row in engineer_rows}
        schedules = []
        if engineer_ids:
            schedules = (
                (
                    await self._session.execute(
                        select(
                            engineer_schedules.c.engineer_id,
                            engineer_schedules.c.work_date,
                            engineer_schedules.c.shift_start,
                            engineer_schedules.c.shift_end,
                        )
                        .where(
                            engineer_schedules.c.engineer_id.in_(engineer_ids),
                            engineer_schedules.c.work_date >= effective_start,
                            engineer_schedules.c.work_date <= maximum_end,
                        )
                        .order_by(
                            engineer_schedules.c.work_date,
                            engineer_schedules.c.engineer_id,
                        )
                    )
                )
                .mappings()
                .all()
            )
        work_type_ids = {int(row.work_type_id) for row in job_rows}
        required_qualifications = await self._pairs(
            work_type_required_qualifications,
            "work_type_id",
            "qualification_id",
            work_type_ids,
        )
        required_equipment = await self._pairs(
            work_type_required_equipment,
            "work_type_id",
            "equipment_type_id",
            work_type_ids,
        )
        engineer_quals = await self._pairs(
            engineer_qualifications,
            "engineer_id",
            "qualification_id",
            engineer_ids,
        )
        equipment_rows = (
            (
                await self._session.execute(
                    select(equipment_types.c.id, equipment_types.c.available_units)
                    .where(
                        equipment_types.c.project_id == project_id,
                        equipment_types.c.active.is_(True),
                    )
                    .order_by(equipment_types.c.id)
                )
            )
            .mappings()
            .all()
        )
        config_snapshot = {
            field: config[field]
            for field in PlanningConfig.__dataclass_fields__
            if field in config
        }
        return _jsonable(
            {
                "project": {
                    "id": project.id,
                    "timezone": project.planning_timezone,
                    "status": project.status,
                    "effective_start_date": effective_start,
                    "maximum_horizon_end": maximum_end,
                },
                "config": config_snapshot,
                "jobs": [dict(row) for row in job_rows],
                "engineers": [dict(row) for row in engineer_rows],
                "schedules": [dict(row) for row in schedules],
                "required_qualifications": required_qualifications,
                "required_equipment": required_equipment,
                "engineer_qualifications": engineer_quals,
                "equipment_units": {
                    str(row.id): int(row.available_units) for row in equipment_rows
                },
            }
        )

    async def _pairs(
        self, table, left_name: str, right_name: str, ids: set[int]
    ) -> dict[str, list[int]]:
        result = {str(value): [] for value in ids}
        if not ids:
            return result
        rows = (
            await self._session.execute(
                select(table).where(table.c[left_name].in_(ids))
            )
        ).mappings()
        for row in rows:
            result[str(row[left_name])].append(int(row[right_name]))
        for values in result.values():
            values.sort()
        return result


def _snapshot_hash(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(_jsonable(snapshot), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "as_tuple"):
        return str(value)
    return value


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _cascade_daily_reason(reason: str) -> str:
    if reason in {"NO_AVAILABLE_ENGINEER", "NO_COMPATIBLE_ENGINEER"}:
        return "NO_AVAILABLE_ENGINEER_TODAY"
    if reason == "INVALID_TIME_WINDOW":
        return "DAILY_TIME_WINDOW_CONFLICT"
    return reason
