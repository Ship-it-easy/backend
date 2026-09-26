import hashlib
import json
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import UUID

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
    planning_routes,
    planning_runs,
    planning_unassigned_jobs,
    project_plan_assignments,
    project_plan_versions,
    projects,
    qualifications,
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
        *,
        effective_start_override: date | None = None,
        excluded_job_ids: set[int] | None = None,
        include_published_jobs: bool = False,
        total_time_limit_override: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        # Authentication/access checks use this request-scoped session first.
        # Close that read transaction and make the complete batch snapshot in a
        # fresh REPEATABLE READ transaction so all source queries see one DB view.
        await self._session.commit()
        await self._session.connection(
            execution_options={"isolation_level": "REPEATABLE READ"}
        )
        snapshot_started_at = datetime.now(timezone.utc)
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
            or not 1 <= int(config.candidate_solver_time_limit_sec) <= 600
            or not 1 <= int(config.single_cascade_time_limit_sec) <= 3600
            or not 1 <= int(config.event_time_limit_sec) <= 3600
            or not 0 <= int(config.event_coalesce_window_sec) <= 120
            or not 1 <= int(config.event_coalesce_max_wait_sec) <= 600
            or int(config.event_coalesce_window_sec)
            > int(config.event_coalesce_max_wait_sec)
            or not 1 <= int(config.max_parallel_candidate_models) <= 32
            or not 1 <= int(config.max_jobs_per_run) <= 1000
            or not 1 <= int(config.max_jobs_per_batch) <= 5000
            or (
                config.travel_cache_ttl_days is not None
                and not 0 <= int(config.travel_cache_ttl_days) <= 3650
            )
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
        effective_start = effective_start_override or await self._effective_start(
            project_id, requested_start_date
        )
        horizon_anchor = (
            requested_start_date
            if effective_start_override is not None
            else effective_start
        )
        maximum_end = horizon_anchor + timedelta(
            days=int(config.batch_maximum_horizon_days) - 1
        )
        snapshot = await self._build_snapshot(
            project_id,
            effective_start,
            maximum_end,
            project,
            config,
            excluded_job_ids=excluded_job_ids,
            include_published_jobs=include_published_jobs,
        )
        if total_time_limit_override is not None:
            snapshot["config"]["batch_total_time_limit_sec"] = min(
                int(snapshot["config"]["batch_total_time_limit_sec"]),
                max(1, int(total_time_limit_override)),
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
                            started_at=snapshot_started_at,
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
                        [
                            {
                                "planning_batch_id": row.id,
                                "planning_date": requested_start_date
                                + timedelta(days=offset),
                                "block_number": offset // 7,
                                "status": "PINNED_PUBLISHED",
                                "finished_at": datetime.now(timezone.utc),
                            }
                            for offset in range(
                                (effective_start - requested_start_date).days
                            )
                        ]
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

    async def save_auxiliary_run(
        self,
        batch_id: int,
        planning_date: date,
        timezone_name: str,
        user_id: Any,
        data: PlanningInput,
        result: PlanningResult,
    ) -> int:
        """Persist an audited solver model that is not a cascade batch day."""

        run_id = await self.create_daily_run(
            batch_id, planning_date, timezone_name, user_id
        )
        try:
            await self.mark_daily_run_running(run_id, data)
            await self._daily.save_result(run_id, data, result)
        except Exception as error:
            await self._daily.fail_run(
                run_id,
                "AUXILIARY_RUN_PERSISTENCE_FAILED",
                str(error),
            )
            raise
        return run_id

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
                    planning_batch_jobs.c.planning_batch_id == bindparam("batch_key"),
                    planning_batch_jobs.c.job_id == bindparam("job_key"),
                )
                .values(
                    priority_group=bindparam("priority_group"),
                    future_opportunity_count=bindparam("future_opportunity_count"),
                    future_opportunity_rank=bindparam("future_opportunity_rank"),
                    future_opportunity_bonus=bindparam("future_opportunity_bonus"),
                    daily_drop_penalty=bindparam("daily_drop_penalty"),
                    cascade_drop_penalty=bindparam("cascade_drop_penalty"),
                    priority_bonus=bindparam("priority_bonus"),
                    solver_drop_cost=bindparam("solver_drop_cost", required=False),
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
                    planning_batch_jobs.c.planning_batch_id == bindparam("batch_key"),
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
                    planning_batch_jobs.c.planning_batch_id == bindparam("batch_key"),
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
                    planning_batches.c.input_snapshot,
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
                        planning_batch_jobs.c.priority_group,
                        planning_batch_jobs.c.future_opportunity_count,
                        planning_batch_jobs.c.cascade_drop_penalty,
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
                        planning_route_jobs.c.planning_run_id,
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
                            planning_runs.c.input_jobs_count,
                            planning_runs.c.eligible_jobs_count,
                            planning_runs.c.assigned_jobs_count,
                            planning_runs.c.unassigned_jobs_count,
                        ).where(planning_runs.c.planning_batch_id == batch_id)
                    )
                )
                .mappings()
                .all()
            )
        }
        errors: list[str] = []
        snapshot = batch.input_snapshot or {}
        if int((snapshot.get("project") or {}).get("id", -1)) != batch.project_id:
            errors.append("snapshot belongs to another project")
        snapshot_job_ids = {int(item["id"]) for item in snapshot.get("jobs", [])}
        state_job_ids = {int(item.job_id) for item in job_states}
        if snapshot_job_ids != state_job_ids:
            errors.append("batch job states do not match the immutable snapshot")
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
            if item.future_opportunity_count is not None and (
                item.future_opportunity_count < 0
            ):
                errors.append(f"job {item.job_id} has an invalid opportunity count")
            if item.cascade_drop_penalty is not None and (
                item.cascade_drop_penalty < 0
            ):
                errors.append(f"job {item.job_id} has an invalid cascade penalty")
        unassigned_counts = Counter(
            int(item.planning_run_id)
            for item in (
                (
                    await self._session.execute(
                        select(planning_unassigned_jobs.c.planning_run_id)
                        .join(
                            planning_runs,
                            planning_runs.c.id
                            == planning_unassigned_jobs.c.planning_run_id,
                        )
                        .where(planning_runs.c.planning_batch_id == batch_id)
                    )
                )
                .mappings()
                .all()
            )
        )
        route_counts = Counter(int(item.planning_run_id) for item in route_assignments)
        ordinary_days = [day for day in day_states if day.status != "PINNED_PUBLISHED"]
        ordinary_dates = {day.planning_date for day in ordinary_days}
        if ordinary_dates:
            last_opened_date = max(ordinary_dates)
            expected_dates = {
                batch.effective_start_date + timedelta(days=offset)
                for offset in range(
                    (last_opened_date - batch.effective_start_date).days + 1
                )
            }
            if ordinary_dates != expected_dates:
                errors.append("opened planning days are not contiguous")
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
                    continue
                run_id = int(day.planning_run_id)
                if route_counts[run_id] != day.assigned_count:
                    errors.append(f"day {day.planning_date} assigned counter disagrees")
                if unassigned_counts[run_id] != day.unassigned_count:
                    errors.append(
                        f"day {day.planning_date} unassigned counter disagrees"
                    )
                if day.assigned_count + day.unassigned_count != day.input_jobs_count:
                    errors.append(f"day {day.planning_date} input counter disagrees")
                if day.solver_candidates_count > day.input_jobs_count:
                    errors.append(
                        f"day {day.planning_date} solver counter exceeds input"
                    )
                if day.dropped_count > day.solver_candidates_count:
                    errors.append(f"day {day.planning_date} dropped counter disagrees")
                if day.deferred_count != day.unassigned_count:
                    errors.append(f"day {day.planning_date} deferred counter disagrees")
                if (
                    run.input_jobs_count != day.input_jobs_count
                    or run.eligible_jobs_count != day.solver_candidates_count
                    or run.assigned_jobs_count != day.assigned_count
                    or run.unassigned_jobs_count != day.unassigned_count
                ):
                    errors.append(f"day {day.planning_date} run counters disagree")
                if not day.input_job_ids_hash:
                    errors.append(
                        f"day {day.planning_date} has no input membership hash"
                    )
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
        foreign_engineer = await self._session.scalar(
            select(planning_routes.c.engineer_id)
            .join(
                planning_runs,
                planning_runs.c.id == planning_routes.c.planning_run_id,
            )
            .join(engineers, engineers.c.id == planning_routes.c.engineer_id)
            .where(
                planning_runs.c.planning_batch_id == batch_id,
                engineers.c.project_id != batch.project_id,
            )
            .limit(1)
        )
        if foreign_engineer is not None:
            errors.append("batch contains an engineer from another project")
        work_type_ids = {
            int(item["work_type_id"])
            for item in snapshot.get("jobs", [])
            if item.get("work_type_id") is not None
        }
        qualification_ids = {
            int(value)
            for values in snapshot.get("required_qualifications", {}).values()
            for value in values
        } | {
            int(value)
            for values in snapshot.get("engineer_qualifications", {}).values()
            for value in values
        }
        equipment_ids = {
            int(value)
            for values in snapshot.get("required_equipment", {}).values()
            for value in values
        } | {int(value) for value in snapshot.get("equipment_units", {})}
        for catalog, catalog_ids, label in (
            (work_types, work_type_ids, "work type"),
            (qualifications, qualification_ids, "qualification"),
            (equipment_types, equipment_ids, "equipment type"),
        ):
            if not catalog_ids:
                continue
            foreign_catalog_id = await self._session.scalar(
                select(catalog.c.id)
                .where(
                    catalog.c.id.in_(catalog_ids),
                    catalog.c.project_id != batch.project_id,
                )
                .limit(1)
            )
            if foreign_catalog_id is not None:
                errors.append(f"snapshot contains a foreign {label}")
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
        day_summary = (
            await self._session.execute(
                select(
                    func.count(planning_batch_days.c.id).filter(
                        planning_batch_days.c.status != "PINNED_PUBLISHED"
                    ),
                    func.count(planning_batch_days.c.id).filter(
                        planning_batch_days.c.status == "FAILED"
                    ),
                    func.max(planning_batch_days.c.block_number).filter(
                        planning_batch_days.c.status != "PINNED_PUBLISHED"
                    ),
                ).where(planning_batch_days.c.planning_batch_id == batch_id)
            )
        ).one()
        job_rows = (
            await self._session.execute(
                select(
                    planning_batch_jobs.c.processing_status,
                    planning_batch_jobs.c.snapshot_sla_date,
                    planning_batches.c.effective_start_date,
                )
                .join(
                    planning_batches,
                    planning_batches.c.id == planning_batch_jobs.c.planning_batch_id,
                )
                .where(planning_batch_jobs.c.planning_batch_id == batch_id)
            )
        ).all()
        state_counts = Counter(item.processing_status for item in job_rows)
        metrics = {
            **metrics,
            "eligible_total": len(job_rows),
            "assigned": state_counts["DRAFT_ASSIGNED"],
            "permanent_issues": state_counts["PERMANENT_ISSUE"],
            "remaining": sum(
                count
                for state, count in state_counts.items()
                if state not in {"DRAFT_ASSIGNED", "PERMANENT_ISSUE"}
            ),
            "unassigned_within_horizon": state_counts["UNASSIGNED_WITHIN_HORIZON"],
            "opened_days": int(day_summary[0] or 0),
            "failed_days": int(day_summary[1] or 0),
            "number_of_blocks": int(day_summary[2] or 0),
            "overdue": sum(
                item.snapshot_sla_date < item.effective_start_date for item in job_rows
            ),
        }
        successful_day_exists = (
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
        make_current = status in {"SUCCESS", "PARTIAL"} and not (
            completion_reason == "STOPPED_BY_USER" and not successful_day_exists
        )
        if make_current:
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
                current_flag=make_current,
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
                    "FAILED" if validation_failed or not successful_days else "PARTIAL"
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
                        jobs.c.latitude,
                        jobs.c.longitude,
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
        engineer_details = {
            int(row.id): row
            for row in (
                (
                    await self._session.execute(
                        select(
                            engineers.c.id,
                            engineers.c.name,
                            engineers.c.transport_type,
                            engineers.c.start_latitude,
                            engineers.c.start_longitude,
                        ).where(engineers.c.project_id == project_id)
                    )
                )
                .mappings()
                .all()
            )
        }
        equipment_names = {
            int(row.id): str(row.name)
            for row in (
                (
                    await self._session.execute(
                        select(equipment_types.c.id, equipment_types.c.name).where(
                            equipment_types.c.project_id == project_id
                        )
                    )
                )
                .mappings()
                .all()
            )
        }
        job_addresses = {int(row.job_id): str(row.address) for row in job_rows}
        job_coordinates = {
            int(row.job_id): {
                "latitude": row.latitude,
                "longitude": row.longitude,
            }
            for row in job_rows
            if row.latitude is not None and row.longitude is not None
        }
        day_values = []
        for day in days:
            value = dict(day)
            if day.planning_run_id is not None and day.status == "SUCCESS":
                run = await self._daily.get_run(project_id, int(day.planning_run_id))
                value["routes"] = run["routes"]
                value["unassigned_jobs"] = run["unassigned_jobs"]
                for route in value["routes"]:
                    engineer = engineer_details.get(int(route["engineer_id"]))
                    route["engineer_name"] = (
                        str(engineer.name) if engineer is not None else "Инженер"
                    )
                    route["transport_type"] = (
                        str(engineer.transport_type) if engineer is not None else "NONE"
                    )
                    route["start_coordinate"] = (
                        {
                            "latitude": engineer.start_latitude,
                            "longitude": engineer.start_longitude,
                        }
                        if engineer is not None
                        and engineer.start_latitude is not None
                        and engineer.start_longitude is not None
                        else None
                    )
                    route["equipment"] = [
                        equipment_names[equipment_id]
                        for equipment_id in route["equipment_type_ids"]
                        if equipment_id in equipment_names
                    ]
                    for job in route["jobs"]:
                        job["address"] = job_addresses.get(
                            int(job["job_id"]), "Адрес не указан"
                        )
                        job["coordinate"] = job_coordinates.get(int(job["job_id"]))
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
        ordinary_days = [item for item in days if item.status != "PINNED_PUBLISHED"]
        active_day = next(
            (item for item in ordinary_days if item.status == "RUNNING"),
            next((item for item in ordinary_days if item.status == "PENDING"), None),
        )
        payload = dict(batch)
        payload.pop("input_snapshot", None)
        payload.update(
            days=day_values,
            backlog=backlog,
            progress={
                "processed_days": processed,
                "opened_days": len(ordinary_days),
                "failed_days": sum(1 for item in days if item.status == "FAILED"),
                "assigned": assigned,
                "remaining": sum(
                    1
                    for item in job_rows
                    if item.processing_status
                    not in {"DRAFT_ASSIGNED", "PERMANENT_ISSUE"}
                ),
                "current_planning_date": (
                    active_day.planning_date if active_day is not None else None
                ),
                "current_block": (
                    active_day.block_number if active_day is not None else None
                ),
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
        *,
        excluded_job_ids: set[int] | None = None,
        include_published_jobs: bool = False,
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
        job_filters = [
            jobs.c.project_id == project_id,
            work_types.c.project_id == project_id,
            jobs.c.status == "NEW",
            jobs.c.sla_date <= maximum_end,
        ]
        if not include_published_jobs:
            job_filters.append(~active_assignment)
        if excluded_job_ids:
            job_filters.append(jobs.c.id.not_in(excluded_job_ids))
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
                        jobs.c.received_at,
                        jobs.c.ingest_sequence,
                        jobs.c.created_at,
                        jobs.c.updated_at,
                        work_types.c.priority.label("priority"),
                        work_types.c.name.label("work_type_name"),
                        work_types.c.default_service_duration_min,
                        work_types.c.required_transport,
                    )
                    .join(work_types, jobs.c.work_type_id == work_types.c.id)
                    .where(*job_filters)
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
                        engineers.c.name,
                        engineers.c.transport_type,
                        engineers.c.start_address,
                        engineers.c.start_latitude,
                        engineers.c.start_longitude,
                        engineers.c.created_at,
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
        if include_published_jobs:
            published_work_type_ids = (
                await self._session.scalars(
                    select(jobs.c.work_type_id)
                    .join(
                        project_plan_assignments,
                        project_plan_assignments.c.job_id == jobs.c.id,
                    )
                    .join(
                        project_plan_versions,
                        project_plan_versions.c.id
                        == project_plan_assignments.c.plan_version_id,
                    )
                    .where(
                        project_plan_versions.c.project_id == project_id,
                        project_plan_versions.c.is_current.is_(True),
                        project_plan_assignments.c.planning_date >= effective_start,
                        project_plan_assignments.c.planning_date <= maximum_end,
                    )
                )
            ).all()
            work_type_ids.update(int(value) for value in published_work_type_ids)
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
                    select(
                        equipment_types.c.id,
                        equipment_types.c.name,
                        equipment_types.c.available_units,
                    )
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
                "equipment_types": [dict(row) for row in equipment_rows],
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
    if isinstance(value, UUID):
        return str(value)
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
