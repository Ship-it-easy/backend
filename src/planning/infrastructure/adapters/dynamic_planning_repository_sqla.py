import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from time import monotonic
from typing import Any

from sqlalchemy import case, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.infrastructure.persistence_sqla.mappings.user import users_table
from planning.application.services.planning_board import (
    assigned_primary_reason,
    reason,
    unassigned_reason,
    unassigned_sort_key,
)
from planning.application.validators.dynamic_plan import DynamicPlanValidator
from planning.infrastructure.adapters.planning_batch_repository_sqla import (
    SqlaPlanningBatchRepository,
    _jsonable,
)
from planning.infrastructure.persistence_sqla.mappings.tables import (
    candidate_evaluations,
    engineer_schedules,
    engineers,
    equipment_types,
    job_planning_state,
    job_status_history,
    jobs,
    plan_changes,
    planning_baseline_results,
    planning_batch_days,
    planning_batch_jobs,
    planning_batches,
    planning_cancelled_job_snapshots,
    planning_day_results,
    planning_events,
    planning_plan_comparisons,
    planning_route_jobs,
    planning_routes,
    planning_runs,
    planning_unassigned_jobs,
    project_plan_assignments,
    project_plan_versions,
    projects,
    work_types,
)

logger = logging.getLogger(__name__)


class StaleDynamicSnapshot(RuntimeError):
    pass


class SqlaDynamicPlanningRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_active_event(self, project_id: int) -> dict[str, Any] | None:
        row = (
            (
                await self._session.execute(
                    select(planning_events)
                    .where(
                        planning_events.c.project_id == project_id,
                        planning_events.c.state.in_(("PENDING", "RUNNING")),
                    )
                    .order_by(planning_events.c.id)
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        return _jsonable(dict(row)) if row is not None else None

    async def enqueue(
        self,
        project_id: int,
        event_type: str,
        actor_user_id: Any,
        idempotency_key: str,
        job_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        if event_type == "MANUAL":
            # Serialize the read/create pair for manual starts. The transaction
            # lock closes the race where two UI requests both observe no active
            # event and enqueue duplicate calculations for the same project.
            await self._session.execute(select(func.pg_advisory_xact_lock(project_id)))
            active = await self.get_active_event(project_id)
            if active is not None:
                return {**active, "_reused_active": True}
        statement = (
            pg_insert(planning_events)
            .values(
                project_id=project_id,
                event_type=event_type,
                job_ids=job_ids or [],
                initiator="USER" if actor_user_id else "SYSTEM",
                actor_user_id=actor_user_id,
                idempotency_key=idempotency_key,
                state="PENDING",
            )
            .on_conflict_do_nothing(
                index_elements=[
                    planning_events.c.project_id,
                    planning_events.c.idempotency_key,
                ]
            )
            .returning(*planning_events.c)
        )
        row = (await self._session.execute(statement)).mappings().one_or_none()
        if row is None:
            row = (
                (
                    await self._session.execute(
                        select(planning_events).where(
                            planning_events.c.project_id == project_id,
                            planning_events.c.idempotency_key == idempotency_key,
                        )
                    )
                )
                .mappings()
                .one()
            )
        return {**_jsonable(dict(row)), "_reused_active": False}

    async def claim_pending(
        self,
        project_id: int,
        *,
        coalesce_window_sec: int,
        coalesce_max_wait_sec: int,
    ) -> list[dict[str, Any]]:
        project_status = await self._session.scalar(
            select(projects.c.status).where(projects.c.id == project_id)
        )
        rows = (
            (
                await self._session.execute(
                    select(planning_events)
                    .where(
                        planning_events.c.project_id == project_id,
                        planning_events.c.state == "PENDING",
                    )
                    .order_by(planning_events.c.requested_at, planning_events.c.id)
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            return []
        ids = [int(row.id) for row in rows]
        if project_status != "ACTIVE":
            await self._session.execute(
                update(planning_events)
                .where(planning_events.c.id.in_(ids))
                .values(
                    state="FAILED",
                    error_code="PROJECT_BLOCKED",
                    error_message="Project is blocked",
                    finished_at=datetime.now(timezone.utc),
                )
            )
            await self._session.commit()
            return []
        now = datetime.now(timezone.utc)
        oldest = min(row.requested_at for row in rows)
        newest = max(row.requested_at for row in rows)
        quiet_seconds = (now - newest).total_seconds()
        total_wait_seconds = (now - oldest).total_seconds()
        if (
            quiet_seconds < coalesce_window_sec
            and total_wait_seconds < coalesce_max_wait_sec
        ):
            return []
        started = datetime.now(timezone.utc)
        await self._session.execute(
            update(planning_events)
            .where(planning_events.c.id.in_(ids))
            .values(
                state="RUNNING",
                started_at=started,
                attempt_count=planning_events.c.attempt_count + 1,
                error_code=None,
                error_message=None,
            )
        )
        await self._session.commit()
        for row in rows:
            logger.info(
                "planning_run_phase_changed project_id=%s event_id=%s phase=RUNNING",
                project_id,
                row.id,
            )
        return [_jsonable(dict(row)) for row in rows]

    async def set_candidate_progress(
        self,
        event_id: int,
        *,
        total: int,
        completed: int,
        current_engineer_id: int | None,
    ) -> None:
        await self._session.execute(
            update(planning_events)
            .where(planning_events.c.id == event_id)
            .values(
                candidate_total=total,
                candidate_completed=completed,
                current_candidate_engineer_id=current_engineer_id,
            )
        )
        await self._session.commit()

    async def select_candidate(
        self, event_id: int, subject_job_id: int, engineer_id: int
    ) -> None:
        await self._session.execute(
            update(candidate_evaluations)
            .where(
                candidate_evaluations.c.planning_event_id == event_id,
                candidate_evaluations.c.subject_job_id == subject_job_id,
            )
            .values(selected=False)
        )
        await self._session.execute(
            update(candidate_evaluations)
            .where(
                candidate_evaluations.c.planning_event_id == event_id,
                candidate_evaluations.c.subject_job_id == subject_job_id,
                candidate_evaluations.c.engineer_id == engineer_id,
            )
            .values(selected=True)
        )
        await self.set_candidate_progress(
            event_id,
            total=await self._session.scalar(
                select(planning_events.c.candidate_total).where(
                    planning_events.c.id == event_id
                )
            )
            or 0,
            completed=await self._session.scalar(
                select(planning_events.c.candidate_completed).where(
                    planning_events.c.id == event_id
                )
            )
            or 0,
            current_engineer_id=None,
        )

    async def load_context(
        self, project_id: int, planning_date: date
    ) -> dict[str, Any]:
        batch_repository = SqlaPlanningBatchRepository(self._session)
        project = (
            (
                await self._session.execute(
                    select(projects).where(projects.c.id == project_id)
                )
            )
            .mappings()
            .one()
        )
        config = await batch_repository._active_config(project_id)
        if config is None:
            raise RuntimeError("Project has no active planning configuration")
        maximum_end = planning_date + timedelta(
            days=int(config.batch_maximum_horizon_days) - 1
        )
        source = await batch_repository._build_snapshot(
            project_id,
            planning_date,
            maximum_end,
            project,
            config,
            include_published_jobs=True,
        )
        current_version = (
            (
                await self._session.execute(
                    select(project_plan_versions).where(
                        project_plan_versions.c.project_id == project_id,
                        project_plan_versions.c.is_current.is_(True),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        block_anchor_date = planning_date
        if (
            current_version is not None
            and current_version.planning_batch_id is not None
        ):
            published_batch_date = await self._session.scalar(
                select(planning_batches.c.requested_start_date).where(
                    planning_batches.c.id == current_version.planning_batch_id
                )
            )
            if published_batch_date is not None:
                block_anchor_date = published_batch_date
        assignment_rows: list[Any] = []
        if current_version is not None:
            actual_started_at = (
                select(func.max(job_status_history.c.created_at))
                .where(
                    job_status_history.c.job_id == jobs.c.id,
                    job_status_history.c.new_status == "IN_PROGRESS",
                )
                .correlate(jobs)
                .scalar_subquery()
            )
            actual_completed_at = (
                select(func.max(job_status_history.c.created_at))
                .where(
                    job_status_history.c.job_id == jobs.c.id,
                    job_status_history.c.new_status == "COMPLETED",
                )
                .correlate(jobs)
                .scalar_subquery()
            )
            assignment_rows = (
                (
                    await self._session.execute(
                        select(
                            project_plan_assignments,
                            jobs.c.status,
                            work_types.c.priority.label("priority"),
                            jobs.c.address,
                            jobs.c.latitude,
                            jobs.c.longitude,
                            jobs.c.sla_date,
                            jobs.c.time_window_start,
                            jobs.c.time_window_end,
                            jobs.c.work_type_id,
                            jobs.c.service_duration_min,
                            jobs.c.created_at.label("job_created_at"),
                            jobs.c.previous_status,
                            jobs.c.cancelled_at,
                            work_types.c.default_service_duration_min,
                            work_types.c.required_transport,
                            actual_started_at.label("actual_started_at"),
                            actual_completed_at.label("actual_completed_at"),
                        )
                        .join(jobs, jobs.c.id == project_plan_assignments.c.job_id)
                        .join(work_types, work_types.c.id == jobs.c.work_type_id)
                        .where(
                            project_plan_assignments.c.plan_version_id
                            == current_version.id,
                            project_plan_assignments.c.planning_date.between(
                                planning_date, maximum_end
                            ),
                        )
                        .order_by(
                            project_plan_assignments.c.planning_date,
                            project_plan_assignments.c.engineer_id,
                            project_plan_assignments.c.sequence,
                        )
                    )
                )
                .mappings()
                .all()
            )
        current_assignments = [_jsonable(dict(row)) for row in assignment_rows]
        assignments = [
            item
            for item in current_assignments
            if _as_date(item["planning_date"]) == planning_date
        ]
        fingerprint = self._fingerprint(current_version, assignments)
        source_hash = hashlib.sha256(
            json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "project": _jsonable(dict(project)),
            "source": source,
            "current_version_id": (
                int(current_version.id) if current_version is not None else None
            ),
            "current_version_number": (
                int(current_version.version_number)
                if current_version is not None
                else 0
            ),
            "today_assignments": assignments,
            "current_assignments": current_assignments,
            "block_anchor_date": block_anchor_date,
            "fingerprint": fingerprint,
            "source_hash": source_hash,
            "snapshot_time": datetime.now(timezone.utc),
            "maximum_end": maximum_end,
        }

    async def save_candidate(self, values: dict[str, Any]) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        await self._session.execute(
            pg_insert(candidate_evaluations)
            .values(**_jsonable(values))
            .on_conflict_do_update(
                index_elements=[
                    candidate_evaluations.c.project_id,
                    candidate_evaluations.c.planning_event_id,
                    candidate_evaluations.c.subject_job_id,
                    candidate_evaluations.c.engineer_id,
                ],
                set_={
                    key: _jsonable(value)
                    for key, value in values.items()
                    if key
                    not in {
                        "project_id",
                        "planning_event_id",
                        "subject_job_id",
                        "engineer_id",
                    }
                },
            )
        )
        await self._session.commit()

    async def attach_batch(self, event_ids: list[int], batch_id: int) -> None:
        event_rows = (
            (
                await self._session.execute(
                    select(
                        planning_events.c.id,
                        planning_events.c.event_type,
                        planning_events.c.job_ids,
                        planning_events.c.engineer_ids,
                        planning_events.c.initiator,
                        planning_events.c.actor_user_id,
                        planning_events.c.idempotency_key,
                        planning_events.c.event_payload,
                        planning_events.c.requested_at.label("created_at"),
                    ).where(planning_events.c.id.in_(event_ids))
                )
            )
            .mappings()
            .all()
        )
        snapshot = await self._session.scalar(
            select(planning_batches.c.input_snapshot).where(
                planning_batches.c.id == batch_id
            )
        )
        if snapshot is not None:
            enriched_snapshot = dict(snapshot)
            enriched_snapshot["source_events"] = [
                _jsonable(dict(item)) for item in event_rows
            ]
            await self._session.execute(
                update(planning_batches)
                .where(planning_batches.c.id == batch_id)
                .values(input_snapshot=enriched_snapshot)
            )
        await self._session.execute(
            update(planning_events)
            .where(planning_events.c.id.in_(event_ids))
            .values(planning_batch_id=batch_id)
        )
        await self._session.execute(
            update(candidate_evaluations)
            .where(candidate_evaluations.c.planning_event_id.in_(event_ids))
            .values(planning_batch_id=batch_id)
        )
        await self._session.commit()

    async def complete(
        self,
        event_ids: list[int],
        version_id: int,
        input_hash: str,
    ) -> None:
        event_rows = (
            (
                await self._session.execute(
                    select(
                        planning_events.c.id,
                        planning_events.c.project_id,
                        planning_events.c.requested_at,
                    ).where(planning_events.c.id.in_(event_ids))
                )
            )
            .mappings()
            .all()
        )
        finished_at = datetime.now(timezone.utc)
        await self._session.execute(
            update(planning_events)
            .where(planning_events.c.id.in_(event_ids))
            .values(
                state="PUBLISHED",
                published_plan_version_id=version_id,
                input_hash=input_hash,
                finished_at=finished_at,
            )
        )
        await self._session.commit()
        for event in event_rows:
            logger.info(
                "planning_run_finished project_id=%s event_id=%s "
                "plan_version_id=%s state=PUBLISHED duration_ms=%s",
                event.project_id,
                event.id,
                version_id,
                round((finished_at - event.requested_at).total_seconds() * 1000),
            )

    async def fail(
        self, event_ids: list[int], code: str, message: str, *, retry: bool = False
    ) -> None:
        await self._session.rollback()
        finished_at = datetime.now(timezone.utc)
        rows = (
            (
                await self._session.execute(
                    update(planning_events)
                    .where(planning_events.c.id.in_(event_ids))
                    .values(
                        state="PENDING" if retry else "FAILED",
                        error_code=code,
                        error_message=message[:1000],
                        finished_at=None if retry else finished_at,
                    )
                    .returning(
                        planning_events.c.id,
                        planning_events.c.project_id,
                        planning_events.c.requested_at,
                    )
                )
            )
            .mappings()
            .all()
        )
        await self._session.commit()
        for event in rows:
            if retry:
                logger.warning(
                    "planning_run_retry_scheduled project_id=%s event_id=%s "
                    "error_code=%s",
                    event.project_id,
                    event.id,
                    code,
                )
            else:
                logger.error(
                    "planning_run_finished project_id=%s event_id=%s state=FAILED "
                    "error_code=%s duration_ms=%s",
                    event.project_id,
                    event.id,
                    code,
                    round((finished_at - event.requested_at).total_seconds() * 1000),
                )

    async def publish(
        self,
        *,
        project_id: int,
        event_ids: list[int],
        batch_id: int,
        planning_date: date,
        timezone_name: str,
        expected_fingerprint: str,
        expected_source_hash: str,
        today_assignments: list[dict[str, Any]],
        trigger_source: str,
        actor_user_id: Any,
    ) -> tuple[int, str]:
        current_date = (
            datetime.now(timezone.utc)
            .astimezone(__import__("zoneinfo").ZoneInfo(timezone_name))
            .date()
        )
        if current_date != planning_date:
            raise StaleDynamicSnapshot("Project date changed during calculation")
        await self._session.execute(
            select(projects.c.id).where(projects.c.id == project_id).with_for_update()
        )
        # Keep every table that contributes to the planning snapshot stable
        # until the new project plan version is committed. SHARE permits
        # concurrent reads but blocks source mutations, closing the gap between
        # the final hash check and publication.
        await self._session.execute(
            text(
                "LOCK TABLE projects, planning_config, jobs, work_types, "
                "engineers, engineer_schedules, engineer_qualifications, "
                "work_type_required_qualifications, "
                "work_type_required_equipment, equipment_types, "
                "job_status_history IN SHARE MODE"
            )
        )
        fresh_context = await self.load_context(project_id, planning_date)
        if fresh_context["source_hash"] != expected_source_hash:
            raise StaleDynamicSnapshot("Planning inputs changed during calculation")
        current_version = (
            (
                await self._session.execute(
                    select(project_plan_versions).where(
                        project_plan_versions.c.project_id == project_id,
                        project_plan_versions.c.is_current.is_(True),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        current_today = await self._current_today(current_version, planning_date)
        if self._fingerprint(current_version, current_today) != expected_fingerprint:
            raise StaleDynamicSnapshot("Published plan or protected statuses changed")

        batch = (
            (
                await self._session.execute(
                    select(planning_batches).where(planning_batches.c.id == batch_id)
                )
            )
            .mappings()
            .one()
        )
        if batch.status not in {
            "SUCCESS",
            "PARTIAL",
        } or batch.completion_reason not in {
            "ALL_ELIGIBLE_ASSIGNED",
            "NO_FUTURE_OPPORTUNITIES",
            "HORIZON_LIMIT",
            "NO_ELIGIBLE_JOBS",
        }:
            failed_run = (
                (
                    await self._session.execute(
                        select(
                            planning_runs.c.error_code,
                            planning_runs.c.error_message,
                        )
                        .where(
                            planning_runs.c.planning_batch_id == batch_id,
                            planning_runs.c.status.in_(("FAILED", "FAILED_VALIDATION")),
                        )
                        .order_by(planning_runs.c.id.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if failed_run is not None:
                raise RuntimeError(
                    str(failed_run.error_message or failed_run.error_code)
                )
            raise RuntimeError("The future cascade did not finish publishably")

        preserved_rows: list[Any] = []
        if current_version is not None and batch.effective_start_date > planning_date:
            preserved_rows = (
                (
                    await self._session.execute(
                        select(project_plan_assignments)
                        .join(jobs, jobs.c.id == project_plan_assignments.c.job_id)
                        .where(
                            project_plan_assignments.c.plan_version_id
                            == current_version.id,
                            project_plan_assignments.c.planning_date > planning_date,
                            project_plan_assignments.c.planning_date
                            < batch.effective_start_date,
                            jobs.c.status != "CANCELLED",
                        )
                    )
                )
                .mappings()
                .all()
            )
        future_rows = (
            (
                await self._session.execute(
                    select(
                        planning_route_jobs.c.job_id,
                        planning_runs.c.planning_date,
                        planning_routes.c.engineer_id,
                        planning_route_jobs.c.sequence,
                        planning_route_jobs.c.planned_arrival,
                        planning_route_jobs.c.planned_start,
                        planning_route_jobs.c.planned_finish,
                        planning_route_jobs.c.travel_from_previous_min,
                        planning_route_jobs.c.distance_from_previous_meters,
                        planning_route_jobs.c.waiting_before_job_min,
                    )
                    .join(
                        planning_runs,
                        planning_runs.c.id == planning_route_jobs.c.planning_run_id,
                    )
                    .join(
                        planning_routes,
                        planning_routes.c.id == planning_route_jobs.c.planning_route_id,
                    )
                    .where(
                        planning_runs.c.planning_batch_id == batch_id,
                        planning_runs.c.status == "SUCCESS",
                        planning_runs.c.planning_date > planning_date,
                    )
                )
            )
            .mappings()
            .all()
        )
        assignments = [*_normalize_assignments(today_assignments)]
        assignments.extend(
            _normalize_assignments([dict(row) for row in preserved_rows])
        )
        assignments.extend(_normalize_assignments([dict(row) for row in future_rows]))
        validation_errors = DynamicPlanValidator().validate_publication(
            assignments,
            timezone_name=timezone_name,
            minimum_date=planning_date,
            maximum_date=batch.maximum_horizon_end,
        )
        if validation_errors:
            raise RuntimeError("FAILED_VALIDATION: " + "; ".join(validation_errors))
        unassigned_rows = (
            (
                await self._session.execute(
                    select(
                        planning_batch_jobs.c.job_id,
                        planning_batch_jobs.c.primary_reason_code,
                        planning_batch_jobs.c.diagnostic_flags,
                        jobs.c.sla_date,
                    )
                    .join(jobs, jobs.c.id == planning_batch_jobs.c.job_id)
                    .where(
                        planning_batch_jobs.c.planning_batch_id == batch_id,
                        planning_batch_jobs.c.processing_status != "DRAFT_ASSIGNED",
                    )
                )
            )
            .mappings()
            .all()
        )
        unassigned = [
            {
                **_jsonable(dict(row)),
                "sla_risk": row.sla_date <= batch.maximum_horizon_end,
            }
            for row in unassigned_rows
        ]
        outside_rows = (
            (
                await self._session.execute(
                    select(jobs.c.id, jobs.c.sla_date).where(
                        jobs.c.project_id == project_id,
                        jobs.c.status == "NEW",
                        jobs.c.sla_date > batch.maximum_horizon_end,
                    )
                )
            )
            .mappings()
            .all()
        )
        unassigned.extend(
            {
                "job_id": int(row.id),
                "primary_reason_code": "SLA_OUTSIDE_MAXIMUM_HORIZON",
                "diagnostic_flags": {
                    "maximum_horizon_end": batch.maximum_horizon_end.isoformat()
                },
                "sla_date": row.sla_date.isoformat(),
                "sla_risk": False,
            }
            for row in outside_rows
        )
        digest_payload = {
            "batch_input_hash": batch.input_hash,
            "today": assignments,
            "unassigned": unassigned,
        }
        input_hash = hashlib.sha256(
            json.dumps(
                _jsonable(digest_payload), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if current_version is not None and current_version.input_hash == input_hash:
            return int(current_version.id), input_hash

        old = {
            int(row.job_id): _jsonable(dict(row))
            for row in (
                (
                    await self._session.execute(
                        select(project_plan_assignments).where(
                            project_plan_assignments.c.plan_version_id
                            == current_version.id
                        )
                    )
                )
                .mappings()
                .all()
                if current_version is not None
                else []
            )
        }
        next_version = int(current_version.version_number) + 1 if current_version else 1
        if current_version is not None:
            await self._session.execute(
                update(project_plan_versions)
                .where(project_plan_versions.c.id == current_version.id)
                .values(is_current=False, superseded_at=datetime.now(timezone.utc))
            )
        version_id = int(
            await self._session.scalar(
                insert(project_plan_versions)
                .values(
                    project_id=project_id,
                    version_number=next_version,
                    planning_batch_id=batch_id,
                    planning_event_id=event_ids[0],
                    input_hash=input_hash,
                    trigger_source=trigger_source,
                    actor_user_id=actor_user_id,
                    is_current=True,
                    unassigned_jobs=unassigned,
                    metrics=batch.metrics,
                )
                .returning(project_plan_versions.c.id)
            )
        )
        if assignments:
            await self._session.execute(
                insert(project_plan_assignments),
                [
                    {
                        **item,
                        "plan_version_id": version_id,
                        "project_id": project_id,
                    }
                    for item in assignments
                ],
            )
        new = {int(item["job_id"]): item for item in assignments}
        changes = _changes(old, new, version_id, project_id)
        unassigned_by_job = {int(item["job_id"]): item for item in unassigned}
        for change in changes:
            if change["change_type"] != "DISPLACED":
                continue
            unassigned_job = unassigned_by_job.get(int(change["job_id"]))
            if unassigned_job and unassigned_job.get("primary_reason_code"):
                change["reason"] = str(unassigned_job["primary_reason_code"])
        event_rows = (
            (
                await self._session.execute(
                    select(planning_events).where(planning_events.c.id.in_(event_ids))
                )
            )
            .mappings()
            .all()
        )
        cancelled_ids = {
            int(job_id)
            for event in event_rows
            if event.event_type == "JOB_CANCELLED"
            for job_id in event.job_ids or []
        }
        unavailable_ids = {
            int(job_id)
            for event in event_rows
            if event.event_type == "ENGINEER_AVAILABILITY_LOST"
            for job_id in event.job_ids or []
        }
        changed_jobs = [
            change
            for change in changes
            if change.get("old_assignment") != change.get("new_assignment")
        ]
        reassigned_jobs = [
            change
            for change in changed_jobs
            if change.get("old_assignment")
            and change.get("new_assignment")
            and (
                change["old_assignment"].get("engineer_id")
                != change["new_assignment"].get("engineer_id")
                or change["old_assignment"].get("planning_date")
                != change["new_assignment"].get("planning_date")
            )
        ]
        route_distances: dict[tuple[str, int], int] = {}
        for item in assignments:
            key = (str(item["planning_date"]), int(item["engineer_id"]))
            route_distances[key] = route_distances.get(key, 0) + int(
                item.get("distance_from_previous_meters") or 0
            )
        run_rows = (
            (
                await self._session.execute(
                    select(
                        planning_runs.c.id,
                        planning_runs.c.planning_date,
                        planning_runs.c.solver_status,
                        planning_runs.c.objective,
                        planning_runs.c.solver_time_ms,
                        planning_runs.c.fixed_active_engineer_count,
                        planning_runs.c.newly_activated_engineer_count,
                        planning_runs.c.used_engineer_count,
                        planning_runs.c.total_distance_meters,
                        planning_runs.c.max_engineer_distance_meters,
                        planning_runs.c.objective_range_snapshot,
                    ).where(planning_runs.c.planning_batch_id == batch_id)
                )
            )
            .mappings()
            .all()
        )
        version_planning_metrics = _planning_metrics(
            assignments, [item.planning_date for item in run_rows]
        )
        publication_metrics = {
            **(batch.metrics or {}),
            "source_event_count": len(event_ids),
            "coalesced_event_count": max(0, len(event_ids) - 1),
            "cancelled_count": len(cancelled_ids),
            "removed_unavailable_count": sum(
                change["job_id"] in unavailable_ids
                and change.get("old_assignment") is not None
                for change in changed_jobs
            ),
            "reassigned_count": len(reassigned_jobs),
            "unassigned_count": len(unassigned),
            "active_engineers_before": len(
                {
                    int(item["engineer_id"])
                    for item in old.values()
                    if item.get("engineer_id") is not None
                }
            ),
            "active_engineers_after": len(
                {int(item["engineer_id"]) for item in assignments}
            ),
            "total_distance_meters": sum(route_distances.values()),
            "maximum_route_distance_meters": max(route_distances.values(), default=0),
            "solver_time_ms": sum(int(item.solver_time_ms or 0) for item in run_rows),
            "feasible_time_limit": any(
                item.solver_status == "FEASIBLE_TIME_LIMIT" for item in run_rows
            ),
            "solver_runs": [_jsonable(dict(item)) for item in run_rows],
            "route_distances": [
                {
                    "planning_date": planning_day,
                    "engineer_id": engineer_id,
                    "distance_meters": distance_meters,
                }
                for (planning_day, engineer_id), distance_meters in sorted(
                    route_distances.items()
                )
            ],
            # Keep an immutable operational breakdown on the version itself.
            # The same values can be rebuilt from project_plan_assignments for
            # legacy versions, but persisting them makes exports and audits
            # independent from the read-model implementation.
            "daily_metrics": version_planning_metrics["days"],
            "engineer_metrics": version_planning_metrics["engineers"],
        }
        await self._session.execute(
            update(project_plan_versions)
            .where(project_plan_versions.c.id == version_id)
            .values(metrics=_jsonable(publication_metrics))
        )
        for change in changes:
            if change["job_id"] in cancelled_ids:
                change["change_type"] = "CANCELLED"
                change["reason"] = "JOB_CANCELLED"
            elif change["job_id"] in unavailable_ids and change["old_assignment"]:
                change["reason"] = "ENGINEER_UNAVAILABLE"
        if changes:
            await self._session.execute(insert(plan_changes), changes)
        await self._save_planning_board_read_model(
            previous_version_id=(
                int(current_version.id) if current_version is not None else None
            ),
            version_id=version_id,
            project_id=project_id,
            batch_id=batch_id,
            changes=changes,
        )
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        assigned_ids = set(new)
        planning_rows = []
        for item in assignments:
            planning_rows.append(
                {
                    "job_id": int(item["job_id"]),
                    "project_id": project_id,
                    "state": "ASSIGNED",
                    "reason_code": None,
                    "source_event_id": event_ids[0],
                    "plan_version_id": version_id,
                }
            )
        for item in unassigned:
            job_id = int(item["job_id"])
            if job_id in assigned_ids:
                continue
            planning_rows.append(
                {
                    "job_id": job_id,
                    "project_id": project_id,
                    "state": "UNASSIGNED",
                    "reason_code": (
                        "ENGINEER_UNAVAILABLE"
                        if job_id in unavailable_ids
                        else item.get("primary_reason_code")
                    ),
                    "source_event_id": event_ids[0],
                    "plan_version_id": version_id,
                }
            )
        for state_row in planning_rows:
            await self._session.execute(
                pg_insert(job_planning_state)
                .values(**state_row)
                .on_conflict_do_update(
                    index_elements=[job_planning_state.c.job_id],
                    set_={
                        key: value
                        for key, value in state_row.items()
                        if key != "job_id"
                    },
                )
            )
        return version_id, input_hash

    async def _save_planning_board_read_model(
        self,
        *,
        previous_version_id: int | None,
        version_id: int,
        project_id: int,
        batch_id: int,
        changes: list[dict[str, Any]],
    ) -> None:
        """Persist immutable day/run links and durable cancellation snapshots."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        if previous_version_id is not None:
            previous_rows = (
                (
                    await self._session.execute(
                        select(planning_day_results).where(
                            planning_day_results.c.plan_version_id
                            == previous_version_id
                        )
                    )
                )
                .mappings()
                .all()
            )
            previous_rows = await self._compatible_day_results(
                previous_rows, version_id=version_id
            )
            if previous_rows:
                await self._session.execute(
                    insert(planning_day_results),
                    [
                        {
                            "plan_version_id": version_id,
                            "project_id": project_id,
                            "planning_date": item.planning_date,
                            "planning_run_id": item.planning_run_id,
                            "input_job_ids_hash": item.input_job_ids_hash,
                            "input_jobs_count": item.input_jobs_count,
                            "assigned_count": item.assigned_count,
                            "unassigned_count": item.unassigned_count,
                            "solver_status": item.solver_status,
                        }
                        for item in previous_rows
                    ],
                )
            previous_cancelled_rows = (
                (
                    await self._session.execute(
                        select(planning_cancelled_job_snapshots).where(
                            planning_cancelled_job_snapshots.c.plan_version_id
                            == previous_version_id
                        )
                    )
                )
                .mappings()
                .all()
            )
            for item in previous_cancelled_rows:
                await self._session.execute(
                    pg_insert(planning_cancelled_job_snapshots)
                    .values(
                        plan_version_id=version_id,
                        project_id=project_id,
                        job_id=item.job_id,
                        planning_date=item.planning_date,
                        engineer_id=item.engineer_id,
                        previous_sequence=item.previous_sequence,
                        planned_start=item.planned_start,
                        planned_finish=item.planned_finish,
                        cancelled_at=item.cancelled_at,
                        cancelled_by_user_id=item.cancelled_by_user_id,
                        cancelled_by_username=item.cancelled_by_username,
                        job_snapshot=item.job_snapshot,
                    )
                    .on_conflict_do_nothing(
                        constraint="uq_planning_cancelled_snapshot_version_job"
                    )
                )

        run_rows = (
            (
                await self._session.execute(
                    select(planning_runs).where(
                        planning_runs.c.planning_batch_id == batch_id,
                        planning_runs.c.status == "SUCCESS",
                    )
                )
            )
            .mappings()
            .all()
        )
        run_ids = [int(run.id) for run in run_rows]
        if run_ids:
            await self._session.execute(
                update(planning_baseline_results)
                .where(
                    planning_baseline_results.c.planning_run_id.in_(run_ids),
                    planning_baseline_results.c.plan_version_id.is_(None),
                )
                .values(plan_version_id=version_id)
            )
        for run in run_rows:
            values = {
                "plan_version_id": version_id,
                "project_id": project_id,
                "planning_date": run.planning_date,
                "planning_run_id": run.id,
                "input_job_ids_hash": run.normalized_input_hash,
                "input_jobs_count": run.input_jobs_count,
                "assigned_count": run.assigned_jobs_count,
                "unassigned_count": run.unassigned_jobs_count,
                "solver_status": run.solver_status,
            }
            await self._session.execute(
                pg_insert(planning_day_results)
                .values(**values)
                .on_conflict_do_update(
                    constraint="uq_planning_day_results_version_date",
                    set_={
                        key: value
                        for key, value in values.items()
                        if key not in {"plan_version_id", "planning_date"}
                    },
                )
            )

        cancelled_changes = [
            item
            for item in changes
            if item.get("change_type") == "CANCELLED" and item.get("old_assignment")
        ]
        if not cancelled_changes:
            return
        cancelled_job_ids = {int(item["job_id"]) for item in cancelled_changes}
        job_rows = (
            (
                await self._session.execute(
                    select(
                        jobs,
                        work_types.c.name.label("work_type_name"),
                        users_table.c.username.label("cancelled_by_username"),
                    )
                    .join(work_types, work_types.c.id == jobs.c.work_type_id)
                    .outerjoin(
                        users_table,
                        users_table.c.id == jobs.c.cancelled_by_user_id,
                    )
                    .where(
                        jobs.c.project_id == project_id,
                        jobs.c.id.in_(cancelled_job_ids),
                    )
                )
            )
            .mappings()
            .all()
        )
        jobs_by_id = {int(item.id): item for item in job_rows}
        for change in cancelled_changes:
            old = change["old_assignment"]
            job = jobs_by_id.get(int(change["job_id"]))
            if job is None:
                continue
            await self._session.execute(
                pg_insert(planning_cancelled_job_snapshots)
                .values(
                    plan_version_id=version_id,
                    project_id=project_id,
                    job_id=change["job_id"],
                    planning_date=_as_date(old["planning_date"]),
                    engineer_id=int(old["engineer_id"]),
                    previous_sequence=old.get("sequence"),
                    planned_start=(
                        _as_datetime(old["planned_start"])
                        if old.get("planned_start")
                        else None
                    ),
                    planned_finish=(
                        _as_datetime(old["planned_finish"])
                        if old.get("planned_finish")
                        else None
                    ),
                    cancelled_at=job.cancelled_at or datetime.now(timezone.utc),
                    cancelled_by_user_id=job.cancelled_by_user_id,
                    cancelled_by_username=job.cancelled_by_username,
                    job_snapshot=_jsonable(dict(job)),
                )
                .on_conflict_do_nothing(
                    constraint="uq_planning_cancelled_snapshot_version_job"
                )
            )

    async def _compatible_day_results(
        self, day_results: list[Any], *, version_id: int
    ) -> list[Any]:
        """Return day results whose assigned outcomes still match a version."""
        if not day_results:
            return []
        run_ids = {int(item.planning_run_id) for item in day_results}
        route_assignment_rows = (
            (
                await self._session.execute(
                    select(
                        planning_route_jobs.c.planning_run_id,
                        planning_route_jobs.c.job_id,
                        planning_routes.c.engineer_id,
                    )
                    .join(
                        planning_routes,
                        planning_routes.c.id == planning_route_jobs.c.planning_route_id,
                    )
                    .where(planning_route_jobs.c.planning_run_id.in_(run_ids))
                )
            )
            .mappings()
            .all()
        )
        route_assignments_by_run: dict[int, list[dict[str, Any]]] = {}
        for item in route_assignment_rows:
            route_assignments_by_run.setdefault(int(item.planning_run_id), []).append(
                dict(item)
            )
        current_assignment_rows = (
            (
                await self._session.execute(
                    select(
                        project_plan_assignments.c.job_id,
                        project_plan_assignments.c.planning_date,
                        project_plan_assignments.c.engineer_id,
                    ).where(project_plan_assignments.c.plan_version_id == version_id)
                )
            )
            .mappings()
            .all()
        )
        current_assignments = {
            int(item.job_id): dict(item) for item in current_assignment_rows
        }
        return [
            item
            for item in day_results
            if _historical_day_result_matches_assignments(
                item,
                route_assignments_by_run.get(int(item.planning_run_id), []),
                current_assignments,
            )
        ]

    async def get_event(self, project_id: int, event_id: int) -> dict[str, Any] | None:
        row = (
            (
                await self._session.execute(
                    select(planning_events).where(
                        planning_events.c.id == event_id,
                        planning_events.c.project_id == project_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        candidate_rows = (
            (
                await self._session.execute(
                    select(candidate_evaluations)
                    .where(candidate_evaluations.c.planning_event_id == event_id)
                    .order_by(
                        candidate_evaluations.c.subject_job_id,
                        candidate_evaluations.c.engineer_id,
                    )
                )
            )
            .mappings()
            .all()
        )
        queued = int(
            await self._session.scalar(
                select(func.count(planning_events.c.id)).where(
                    planning_events.c.project_id == project_id,
                    planning_events.c.state == "PENDING",
                )
            )
            or 0
        )
        day = None
        if row.planning_batch_id is not None:
            day = (
                (
                    await self._session.execute(
                        select(planning_batch_days)
                        .where(
                            planning_batch_days.c.planning_batch_id
                            == row.planning_batch_id
                        )
                        .order_by(
                            case(
                                (planning_batch_days.c.status == "RUNNING", 0),
                                (planning_batch_days.c.status == "PENDING", 1),
                                else_=2,
                            ),
                            planning_batch_days.c.planning_date.asc(),
                        )
                        .limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
        return {
            **_jsonable(dict(row)),
            "candidate_evaluations": [_jsonable(dict(item)) for item in candidate_rows],
            "progress": {
                "candidate_evaluations_completed": int(row.candidate_completed),
                "candidate_evaluations_total": int(row.candidate_total),
                "candidate_evaluations_fraction": (
                    int(row.candidate_completed) / int(row.candidate_total)
                    if row.candidate_total
                    else None
                ),
                "current_candidate_engineer_id": row.current_candidate_engineer_id,
                "queued_events": queued,
                "current_day": _jsonable(dict(day)) if day else None,
            },
        }

    async def get_current_plan(self, project_id: int) -> dict[str, Any] | None:
        version = (
            (
                await self._session.execute(
                    select(project_plan_versions).where(
                        project_plan_versions.c.project_id == project_id,
                        project_plan_versions.c.is_current.is_(True),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if version is None:
            return None
        return await self._plan_version_result(version)

    async def get_planning_board_summary(
        self, project_id: int, from_date: date, days: int
    ) -> dict[str, Any]:
        started = monotonic()
        project = (
            (
                await self._session.execute(
                    select(projects).where(projects.c.id == project_id)
                )
            )
            .mappings()
            .one()
        )
        version = (
            (
                await self._session.execute(
                    select(project_plan_versions).where(
                        project_plan_versions.c.project_id == project_id,
                        project_plan_versions.c.is_current.is_(True),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        event = (
            (
                await self._session.execute(
                    select(planning_events)
                    .where(
                        planning_events.c.project_id == project_id,
                        planning_events.c.state.in_(("PENDING", "RUNNING")),
                    )
                    .order_by(planning_events.c.id.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if event is None:
            failed_query = select(planning_events).where(
                planning_events.c.project_id == project_id,
                planning_events.c.state == "FAILED",
            )
            if version is not None:
                failed_query = failed_query.where(
                    planning_events.c.finished_at >= version.published_at
                )
            event = (
                (
                    await self._session.execute(
                        failed_query.order_by(planning_events.c.id.desc()).limit(1)
                    )
                )
                .mappings()
                .one_or_none()
            )
        actor_name = None
        if version is not None and version.actor_user_id is not None:
            actor_name = await self._session.scalar(
                select(users_table.c.username).where(
                    users_table.c.id == version.actor_user_id
                )
            )
        range_end = from_date + timedelta(days=days - 1)
        day_rows = (
            (
                await self._session.execute(
                    select(planning_day_results).where(
                        planning_day_results.c.plan_version_id
                        == (version.id if version is not None else -1),
                        planning_day_results.c.planning_date >= from_date,
                        planning_day_results.c.planning_date <= range_end,
                    )
                )
            )
            .mappings()
            .all()
        )
        if version is not None:
            day_rows = await self._compatible_day_results(
                day_rows, version_id=int(version.id)
            )
        day_by_date = {item.planning_date: item for item in day_rows}
        assignment_counts: dict[date, int] = {}
        if version is not None:
            assignment_counts = {
                item.planning_date: int(item.count)
                for item in (
                    (
                        await self._session.execute(
                            select(
                                project_plan_assignments.c.planning_date,
                                func.count().label("count"),
                            )
                            .where(
                                project_plan_assignments.c.plan_version_id
                                == version.id,
                                project_plan_assignments.c.planning_date >= from_date,
                                project_plan_assignments.c.planning_date <= range_end,
                            )
                            .group_by(project_plan_assignments.c.planning_date)
                        )
                    )
                    .mappings()
                    .all()
                )
            }
        cancelled_counts: dict[date, int] = {}
        if version is not None:
            cancelled_counts = {
                item.planning_date: int(item.count)
                for item in (
                    (
                        await self._session.execute(
                            select(
                                planning_cancelled_job_snapshots.c.planning_date,
                                func.count().label("count"),
                            )
                            .where(
                                planning_cancelled_job_snapshots.c.plan_version_id
                                == version.id,
                                planning_cancelled_job_snapshots.c.project_id
                                == project_id,
                                planning_cancelled_job_snapshots.c.planning_date
                                >= from_date,
                                planning_cancelled_job_snapshots.c.planning_date
                                <= range_end,
                            )
                            .group_by(planning_cancelled_job_snapshots.c.planning_date)
                        )
                    )
                    .mappings()
                    .all()
                )
            }
        board_days = []
        for offset in range(days):
            planning_date = from_date + timedelta(days=offset)
            saved_day = day_by_date.get(planning_date)
            # A dynamic run can contain only the changed subset while the
            # published version also keeps protected/pinned assignments.
            # The immutable version assignments are therefore authoritative
            # for the number of cards displayed on the board.
            assigned_count = assignment_counts.get(planning_date, 0)
            board_days.append(
                {
                    "date": planning_date.isoformat(),
                    "result_available": saved_day is not None
                    or assigned_count > 0
                    or cancelled_counts.get(planning_date, 0) > 0,
                    "solver_status": (
                        saved_day.solver_status if saved_day is not None else None
                    ),
                    "assigned_count": assigned_count,
                    "unassigned_count": (
                        int(saved_day.unassigned_count) if saved_day is not None else 0
                    ),
                    "cancelled_count": cancelled_counts.get(planning_date, 0),
                }
            )
        selected_day = await self._planning_board_day(
            project_id, from_date, version=version
        )
        version_value = None
        if version is not None:
            batch_status = None
            batch_completion_reason = None
            feasible_time_limit = False
            if version.planning_batch_id is not None:
                batch_row = (
                    (
                        await self._session.execute(
                            select(
                                planning_batches.c.status,
                                planning_batches.c.completion_reason,
                                planning_batches.c.metrics,
                            ).where(planning_batches.c.id == version.planning_batch_id)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if batch_row is not None:
                    batch_status = str(batch_row.status)
                    batch_completion_reason = batch_row.completion_reason
                    feasible_time_limit = bool(
                        (version.metrics or {}).get("feasible_time_limit")
                        or (batch_row.metrics or {}).get("feasible_time_limit")
                    )
            status_value = _published_plan_status(batch_status, feasible_time_limit)
            version_value = {
                "id": int(version.id),
                "number": int(version.version_number),
                "published_at": version.published_at,
                "trigger": version.trigger_source,
                "initiator": actor_name,
                "status": status_value,
                "completion_reason": batch_completion_reason,
                "optimization_limited": feasible_time_limit,
                "unassigned_count": len(version.unassigned_jobs or []),
            }
        active_run = None
        if event is not None:
            current_day = None
            if event.planning_batch_id is not None:
                current_day = (
                    (
                        await self._session.execute(
                            select(planning_batch_days)
                            .where(
                                planning_batch_days.c.planning_batch_id
                                == event.planning_batch_id,
                                planning_batch_days.c.status.in_(
                                    ("RUNNING", "PENDING")
                                ),
                            )
                            .order_by(planning_batch_days.c.planning_date)
                            .limit(1)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            active_run = {
                "id": int(event.id),
                "state": event.state,
                "phase": _planning_phase(event.state, current_day),
                "started_at": event.started_at or event.requested_at,
                "finished_at": event.finished_at,
                "planning_date": (
                    current_day.planning_date if current_day is not None else None
                ),
                "error_code": event.error_code,
                "error_message": _safe_planning_error(event),
            }
        result = {
            "project_date": from_date.isoformat(),
            "timezone": str(project.planning_timezone),
            "plan_version": version_value,
            "active_run": active_run,
            "days": board_days,
            "selected_day": selected_day,
        }
        logger.info(
            "planning_board_opened project_id=%s plan_version_id=%s "
            "from_date=%s duration_ms=%s",
            project_id,
            int(version.id) if version is not None else None,
            from_date,
            round((monotonic() - started) * 1000),
        )
        return result

    async def get_planning_board_day(
        self,
        project_id: int,
        planning_date: date,
        plan_version_id: int | None,
    ) -> dict[str, Any]:
        started = monotonic()
        version = (
            (
                await self._session.execute(
                    select(project_plan_versions).where(
                        project_plan_versions.c.project_id == project_id,
                        project_plan_versions.c.is_current.is_(True),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if version is not None and plan_version_id not in {None, int(version.id)}:
            logger.info(
                "planning_day_response project_id=%s planning_date=%s "
                "status=VERSION_CHANGED duration_ms=%s",
                project_id,
                planning_date,
                round((monotonic() - started) * 1000),
            )
            return {"version_changed": True, "current_plan_version_id": version.id}
        if version is None and plan_version_id is not None:
            logger.info(
                "planning_day_response project_id=%s planning_date=%s "
                "status=VERSION_CHANGED duration_ms=%s",
                project_id,
                planning_date,
                round((monotonic() - started) * 1000),
            )
            return {"version_changed": True, "current_plan_version_id": None}
        result = await self._planning_board_day(
            project_id, planning_date, version=version
        )
        logger.info(
            "planning_day_response project_id=%s plan_version_id=%s "
            "planning_date=%s status=SUCCESS duration_ms=%s",
            project_id,
            result.get("plan_version_id"),
            planning_date,
            round((monotonic() - started) * 1000),
        )
        return result

    async def get_baseline_comparison(
        self, project_id: int, planning_date: date
    ) -> dict[str, Any]:
        version = (
            (
                await self._session.execute(
                    select(project_plan_versions).where(
                        project_plan_versions.c.project_id == project_id,
                        project_plan_versions.c.is_current.is_(True),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        context = {
            "project_id": project_id,
            "date": planning_date.isoformat(),
            "plan_version": int(version.version_number) if version else None,
            "plan_version_id": int(version.id) if version else None,
            "algorithm": {
                "baseline": "FIFO_V2",
                "optimized": "Маршрутная оптимизация OR-Tools",
            },
            "methodology": (
                "FIFO распределяет заявки по времени поступления, каждый раз "
                "проверяя инженеров в фиксированном порядке. Перед каждой "
                "заявкой заложено 20 минут. Оборудование и временные окна не "
                "ограничивают назначение, а фактический пробег берётся из "
                "дорожной матрицы."
            ),
        }
        if version is None:
            return {**context, "status": "NO_DAILY_RESULT"}
        day_result = (
            (
                await self._session.execute(
                    select(planning_day_results).where(
                        planning_day_results.c.plan_version_id == version.id,
                        planning_day_results.c.planning_date == planning_date,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if day_result is None:
            return {**context, "status": "NO_DAILY_RESULT"}
        run = (
            (
                await self._session.execute(
                    select(
                        planning_runs.c.timezone,
                        planning_runs.c.normalized_input_hash,
                        planning_runs.c.input_snapshot,
                        planning_runs.c.input_jobs_count,
                    ).where(planning_runs.c.id == day_result.planning_run_id)
                )
            )
            .mappings()
            .one()
        )
        context = {
            **context,
            "planning_run_id": int(day_result.planning_run_id),
            "timezone": str(run.timezone),
            "input_hash": run.normalized_input_hash,
            "input_jobs_count": int(run.input_jobs_count or 0),
            "input_engineers_count": len(
                (run.input_snapshot or {}).get("engineers", [])
            ),
        }
        baseline = (
            (
                await self._session.execute(
                    select(planning_baseline_results).where(
                        planning_baseline_results.c.project_id == project_id,
                        planning_baseline_results.c.planning_run_id
                        == day_result.planning_run_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if baseline is None:
            return {**context, "status": "NOT_AVAILABLE_LEGACY_PLAN"}
        base = {
            **context,
            "algorithm": {
                **context["algorithm"],
                "baseline": str(baseline.algorithm_version),
            },
            "status": str(baseline.status),
            "calculated_at": baseline.finished_at or baseline.created_at,
            "snapshot_version": baseline.snapshot_version,
            "failure_code": baseline.failure_code,
            "failure_message": baseline.failure_message,
            "earliest_shift_start": baseline.earliest_shift_start,
            "attempt_count": int(baseline.attempt_count or 0),
            "next_retry_at": baseline.next_retry_at,
            "last_attempt_at": baseline.last_attempt_at,
        }
        comparison = (
            (
                await self._session.execute(
                    select(planning_plan_comparisons).where(
                        planning_plan_comparisons.c.project_id == project_id,
                        planning_plan_comparisons.c.planning_run_id
                        == day_result.planning_run_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if comparison is None:
            return _jsonable(base)
        engineers = [
            {key: value for key, value in item.items() if key != "engineer_id"}
            for item in (comparison.engineer_metrics or [])
        ]
        return _jsonable(
            {
                **base,
                "coverage_comparable": bool(comparison.coverage_comparable),
                "baseline": comparison.baseline_metrics,
                "optimized": comparison.optimized_metrics,
                "delta": comparison.deltas,
                "engineers": engineers,
                "formula_version": comparison.formula_version,
            }
        )

    async def _planning_board_day(
        self, project_id: int, planning_date: date, *, version: Any | None
    ) -> dict[str, Any]:
        empty = {
            "date": planning_date.isoformat(),
            "plan_version_id": int(version.id) if version is not None else None,
            "day_result_id": None,
            "result_available": False,
            "solver_status": None,
            "counts": {"assigned": 0, "unassigned": 0, "cancelled": 0},
            "engineer_columns": [],
            "unassigned": {"moved": [], "horizon": []},
            "available_filters": {
                "priorities": ["CRITICAL", "HIGH", "MEDIUM", "LOW"],
                "outcomes": ["ALL", "ASSIGNED", "UNASSIGNED_TODAY"],
                "statuses": [],
            },
        }
        if version is None:
            return empty
        day_result = (
            (
                await self._session.execute(
                    select(planning_day_results).where(
                        planning_day_results.c.plan_version_id == version.id,
                        planning_day_results.c.planning_date == planning_date,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if day_result is not None:
            compatible_results = await self._compatible_day_results(
                [day_result], version_id=int(version.id)
            )
            day_result = compatible_results[0] if compatible_results else None
        batch = None
        batch_snapshot: dict[str, Any] = {}
        version_batch_snapshot: dict[str, Any] = {}
        if version.planning_batch_id is not None:
            batch = (
                (
                    await self._session.execute(
                        select(planning_batches).where(
                            planning_batches.c.id == version.planning_batch_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            batch_snapshot = (batch.input_snapshot or {}) if batch else {}
            version_batch_snapshot = batch_snapshot
        assignment_rows = (
            (
                await self._session.execute(
                    select(
                        project_plan_assignments,
                        jobs.c.status,
                        work_types.c.priority.label("priority"),
                        jobs.c.previous_status,
                        jobs.c.address,
                        jobs.c.sla_date,
                        jobs.c.service_duration_min,
                        jobs.c.time_window_start,
                        jobs.c.time_window_end,
                        jobs.c.created_at.label("job_created_at"),
                        jobs.c.work_type_id,
                        work_types.c.name.label("work_type_name"),
                        work_types.c.default_service_duration_min,
                        engineers.c.name.label("engineer_name"),
                        engineers.c.active.label("engineer_active"),
                        engineers.c.transport_type,
                    )
                    .join(jobs, jobs.c.id == project_plan_assignments.c.job_id)
                    .join(work_types, work_types.c.id == jobs.c.work_type_id)
                    .join(
                        engineers,
                        engineers.c.id == project_plan_assignments.c.engineer_id,
                    )
                    .where(
                        project_plan_assignments.c.plan_version_id == version.id,
                        project_plan_assignments.c.planning_date == planning_date,
                    )
                    .order_by(
                        project_plan_assignments.c.engineer_id,
                        project_plan_assignments.c.sequence,
                    )
                )
            )
            .mappings()
            .all()
        )
        day_row = None
        if version.planning_batch_id is not None:
            day_row = (
                (
                    await self._session.execute(
                        select(planning_batch_days).where(
                            planning_batch_days.c.planning_batch_id
                            == version.planning_batch_id,
                            planning_batch_days.c.planning_date == planning_date,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        run = None
        run_id = (
            int(day_result.planning_run_id)
            if day_result is not None
            else int(day_row.planning_run_id)
            if day_row and day_row.planning_run_id
            else None
        )
        if run_id is None and version.planning_batch_id is not None:
            run_id = await self._session.scalar(
                select(planning_runs.c.id)
                .where(
                    planning_runs.c.planning_batch_id == version.planning_batch_id,
                    planning_runs.c.planning_date == planning_date,
                    planning_runs.c.status == "SUCCESS",
                )
                .order_by(planning_runs.c.id.desc())
                .limit(1)
            )
        if run_id is not None:
            run = (
                (
                    await self._session.execute(
                        select(planning_runs).where(planning_runs.c.id == run_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if (
            run is not None
            and run.planning_batch_id is not None
            and (batch is None or int(batch.id) != int(run.planning_batch_id))
        ):
            source_batch = (
                (
                    await self._session.execute(
                        select(planning_batches).where(
                            planning_batches.c.id == run.planning_batch_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if source_batch is not None:
                batch_snapshot = source_batch.input_snapshot or {}
        assignment_source_contexts = await self._planning_source_contexts(
            project_id=project_id,
            target_version_number=int(version.version_number),
            planning_date=planning_date,
            job_ids={int(item.job_id) for item in assignment_rows},
        )
        final_unassigned_values = list(version.unassigned_jobs or [])
        final_unassigned_ids = {
            int(item["job_id"])
            for item in final_unassigned_values
            if item.get("job_id") is not None
        }
        unassigned_rows = []
        if run is not None:
            unassigned_rows = (
                (
                    await self._session.execute(
                        select(
                            planning_unassigned_jobs,
                            jobs.c.status,
                            work_types.c.priority.label("priority"),
                            jobs.c.address,
                            jobs.c.sla_date,
                            jobs.c.service_duration_min,
                            jobs.c.time_window_start,
                            jobs.c.time_window_end,
                            jobs.c.created_at.label("job_created_at"),
                            jobs.c.work_type_id,
                            work_types.c.name.label("work_type_name"),
                            work_types.c.default_service_duration_min,
                        )
                        .join(jobs, jobs.c.id == planning_unassigned_jobs.c.job_id)
                        .join(work_types, work_types.c.id == jobs.c.work_type_id)
                        .where(planning_unassigned_jobs.c.planning_run_id == run.id)
                    )
                )
                .mappings()
                .all()
            )
        unassigned_rows = _include_final_unassigned_rows(
            unassigned_rows, final_unassigned_values
        )
        cancelled_snapshots = (
            (
                await self._session.execute(
                    select(planning_cancelled_job_snapshots).where(
                        planning_cancelled_job_snapshots.c.plan_version_id
                        == version.id,
                        planning_cancelled_job_snapshots.c.project_id == project_id,
                        planning_cancelled_job_snapshots.c.planning_date
                        == planning_date,
                    )
                )
            )
            .mappings()
            .all()
        )
        input_snapshot = (run.input_snapshot or {}) if run else {}
        run_jobs = {int(item["id"]): item for item in input_snapshot.get("jobs", [])}
        batch_jobs = {int(item["id"]): item for item in batch_snapshot.get("jobs", [])}
        all_job_ids = {
            *(int(item.job_id) for item in assignment_rows),
            *(int(item["job_id"]) for item in unassigned_rows),
            *(int(item.job_id) for item in cancelled_snapshots),
            *run_jobs,
        }
        database_jobs: dict[int, dict[str, Any]] = {}
        if all_job_ids:
            rows = (
                (
                    await self._session.execute(
                        select(
                            jobs,
                            work_types.c.name.label("work_type_name"),
                            work_types.c.default_service_duration_min,
                        )
                        .join(work_types, work_types.c.id == jobs.c.work_type_id)
                        .where(
                            jobs.c.project_id == project_id,
                            jobs.c.id.in_(all_job_ids),
                        )
                    )
                )
                .mappings()
                .all()
            )
            database_jobs = {int(item.id): _jsonable(dict(item)) for item in rows}
            cancelled_user_ids = {
                value["cancelled_by_user_id"]
                for value in database_jobs.values()
                if value.get("cancelled_by_user_id") is not None
            }
            if cancelled_user_ids:
                cancelled_users = (
                    (
                        await self._session.execute(
                            select(users_table.c.id, users_table.c.username).where(
                                users_table.c.id.in_(cancelled_user_ids)
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                usernames = {item.id: item.username for item in cancelled_users}
                for value in database_jobs.values():
                    cancelled_by_user_id = value.get("cancelled_by_user_id")
                    if cancelled_by_user_id is not None:
                        value["cancelled_by_username"] = usernames.get(
                            cancelled_by_user_id
                        )
        future_rows = (
            (
                await self._session.execute(
                    select(
                        project_plan_assignments.c.job_id,
                        project_plan_assignments.c.planning_date,
                    ).where(
                        project_plan_assignments.c.plan_version_id == version.id,
                        project_plan_assignments.c.planning_date > planning_date,
                    )
                )
            )
            .mappings()
            .all()
        )
        later_dates: dict[int, date] = {}
        for item in future_rows:
            current = later_dates.get(int(item.job_id))
            if current is None or item.planning_date < current:
                later_dates[int(item.job_id)] = item.planning_date
        snapshot_equipment_names = {
            int(item["id"]): item.get("name")
            for item in batch_snapshot.get("equipment_types", [])
        }
        database_equipment_names = {
            int(item.id): item.name
            for item in (
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

        def job_snapshot(job_id: int, *, final_horizon: bool = False) -> dict[str, Any]:
            source_context = (
                {} if final_horizon else assignment_source_contexts.get(job_id, {})
            )
            source_batch_snapshot = (
                version_batch_snapshot
                if final_horizon
                else source_context.get("batch_snapshot", batch_snapshot)
            )
            source_run_snapshot = (
                {}
                if final_horizon
                else source_context.get("run_snapshot", input_snapshot)
            )
            source_batch_jobs = {
                int(item["id"]): item for item in source_batch_snapshot.get("jobs", [])
            }
            source_run_jobs = {
                int(item["id"]): item for item in source_run_snapshot.get("jobs", [])
            }
            saved = {
                **source_batch_jobs.get(
                    job_id, {} if final_horizon else batch_jobs.get(job_id, {})
                ),
                **source_run_jobs.get(
                    job_id, {} if final_horizon else run_jobs.get(job_id, {})
                ),
            }
            current = database_jobs.get(job_id, {})
            value = {
                **(saved or current),
                "id": job_id,
            }
            used_current_projection = not saved
            for field in (
                "address",
                "sla_date",
                "priority",
                "service_duration_min",
                "default_service_duration_min",
                "work_type_name",
                "created_at",
            ):
                if value.get(field) is None and current.get(field) is not None:
                    value[field] = current[field]
                    used_current_projection = True
            # Operational status may legitimately advance after publication,
            # but descriptive and explanatory fields always come from the
            # immutable calculation snapshot. Legacy published plans did not
            # always persist that snapshot; for those versions use the best
            # available current projection and explicitly mark it as changed.
            if current.get("status") in {"IN_PROGRESS", "COMPLETED", "CANCELLED"}:
                value["status"] = current["status"]
            value.setdefault("status", saved.get("status", "NEW"))
            duration = (
                value.get("duration_min")
                or value.get("service_duration_min")
                or value.get("default_service_duration_min")
            )
            value["duration_min"] = int(duration or 0)
            source_equipment_names = {
                int(item["id"]): item.get("name")
                for item in source_batch_snapshot.get("equipment_types", [])
            }
            required_equipment_ids = _snapshot_requirement_ids(
                value, source_batch_snapshot, "required_equipment"
            )
            value["required_qualifications"] = _snapshot_requirement_ids(
                value, source_batch_snapshot, "required_qualifications"
            )
            value["required_equipment"] = [
                {
                    "id": equipment_id,
                    "name": source_equipment_names.get(equipment_id)
                    or (
                        None
                        if final_horizon
                        else snapshot_equipment_names.get(equipment_id)
                    )
                    or database_equipment_names.get(equipment_id)
                    or "Название оборудования недоступно",
                }
                for equipment_id in required_equipment_ids
            ]
            snapshot_updated_at = saved.get("updated_at")
            value["current_data_changed"] = bool(
                used_current_projection
                or (
                    snapshot_updated_at
                    and current.get("updated_at")
                    and not _same_instant(snapshot_updated_at, current["updated_at"])
                )
            )
            return _jsonable(value)

        engineer_snapshot = {
            int(item["id"]): item for item in batch_snapshot.get("engineers", [])
        }
        schedule_by_engineer: dict[int, dict[str, Any]] = {}
        source_snapshots = [
            batch_snapshot,
            *(value["batch_snapshot"] for value in assignment_source_contexts.values()),
        ]
        for source_snapshot in source_snapshots:
            for item in source_snapshot.get("engineers", []):
                engineer_snapshot.setdefault(int(item["id"]), item)
            for item in source_snapshot.get("schedules", []):
                if str(item.get("work_date")) == planning_date.isoformat():
                    schedule_by_engineer.setdefault(int(item["engineer_id"]), item)
        engineer_ids = (
            set(schedule_by_engineer)
            | {int(item.engineer_id) for item in assignment_rows}
            | {int(item.engineer_id) for item in cancelled_snapshots}
        )
        current_schedule_ids: set[int] = set()
        if engineer_ids:
            current_schedules = (
                (
                    await self._session.execute(
                        select(engineer_schedules).where(
                            engineer_schedules.c.work_date == planning_date,
                            engineer_schedules.c.engineer_id.in_(engineer_ids),
                        )
                    )
                )
                .mappings()
                .all()
            )
            for item in current_schedules:
                engineer_id = int(item.engineer_id)
                current_schedule_ids.add(engineer_id)
                schedule_by_engineer.setdefault(engineer_id, _jsonable(dict(item)))
        engineer_rows = []
        if engineer_ids:
            engineer_rows = (
                (
                    await self._session.execute(
                        select(engineers).where(
                            engineers.c.project_id == project_id,
                            engineers.c.id.in_(engineer_ids),
                        )
                    )
                )
                .mappings()
                .all()
            )
        engineer_values = {}
        for item in engineer_rows:
            engineer_id = int(item.id)
            saved_engineer = engineer_snapshot.get(engineer_id, {})
            engineer_values[engineer_id] = {
                **saved_engineer,
                "id": engineer_id,
                "name": saved_engineer.get("name") or item.name,
                "transport_type": saved_engineer.get("transport_type")
                or item.transport_type,
                "start_address": saved_engineer.get("start_address")
                or item.start_address,
                "start_latitude": (
                    saved_engineer.get("start_latitude")
                    if saved_engineer.get("start_latitude") is not None
                    else item.start_latitude
                ),
                "start_longitude": (
                    saved_engineer.get("start_longitude")
                    if saved_engineer.get("start_longitude") is not None
                    else item.start_longitude
                ),
                "active": item.active,
            }
        for engineer_id in engineer_ids:
            engineer_values.setdefault(
                engineer_id,
                {
                    **engineer_snapshot.get(engineer_id, {}),
                    "id": engineer_id,
                    "name": "Имя инженера недоступно",
                    "active": False,
                },
            )
        assigned_by_engineer: dict[int, list[dict[str, Any]]] = {}
        statuses: set[str] = set()
        for item in assignment_rows:
            job = job_snapshot(int(item.job_id))
            statuses.add(str(job.get("status") or "NEW"))
            source_input_snapshot = assignment_source_contexts.get(
                int(item.job_id), {}
            ).get("run_snapshot", input_snapshot)
            eligible_count = int(
                source_input_snapshot.get("eligible_engineer_counts", {}).get(
                    str(item.job_id),
                    _eligible_engineer_count(
                        job, source_input_snapshot.get("engineers", [])
                    ),
                )
            )
            penalty = source_input_snapshot.get("penalty_components", {}).get(
                str(item.job_id), {}
            )
            reason_job = {**job, "priority_bonus": penalty.get("priority_bonus")}
            primary = assigned_primary_reason(reason_job, planning_date, eligible_count)
            card = {
                "job_id": int(item.job_id),
                "outcome": "ASSIGNED",
                "route_position": int(item.sequence),
                "planned_start": item.planned_start,
                "planned_end": item.planned_finish,
                "status": job.get("status", "NEW"),
                "priority": job.get("priority", "LOW"),
                "address": job.get("address"),
                "work_type": job.get("work_type_name"),
                "duration_min": job.get("duration_min"),
                "sla_date": job.get("sla_date"),
                "overdue": _is_overdue(job.get("sla_date"), planning_date),
                "required_equipment": job.get("required_equipment", []),
                "current_data_changed": job.get("current_data_changed", False),
                "travel_from_previous_min": item.travel_from_previous_min,
                "distance_from_previous_meters": (item.distance_from_previous_meters),
                "coordinate": _coordinate(job),
                "primary_reason": primary,
            }
            assigned_by_engineer.setdefault(int(item.engineer_id), []).append(card)
        cancelled_by_engineer: dict[int, list[dict[str, Any]]] = {}
        for item in cancelled_snapshots:
            saved_job = item.job_snapshot or {}
            job = {**job_snapshot(int(item.job_id)), **saved_job}
            statuses.add("CANCELLED")
            cancelled_by_engineer.setdefault(int(item.engineer_id), []).append(
                {
                    "job_id": int(item.job_id),
                    "outcome": "CANCELLED",
                    "route_position": None,
                    "previous_route_position": item.previous_sequence,
                    "planned_start": item.planned_start,
                    "planned_end": item.planned_finish,
                    "status": "CANCELLED",
                    "priority": job.get("priority", "LOW"),
                    "address": job.get("address"),
                    "work_type": job.get("work_type_name"),
                    "duration_min": job.get("duration_min"),
                    "sla_date": job.get("sla_date"),
                    "overdue": _is_overdue(job.get("sla_date"), planning_date),
                    "required_equipment": job.get("required_equipment", []),
                    "cancelled_at": item.cancelled_at,
                    "cancelled_by": item.cancelled_by_username,
                    "coordinate": _coordinate(job),
                    "primary_reason": reason("CANCELLED_RECORD"),
                }
            )
        columns = []
        for engineer_id in engineer_ids:
            engineer = engineer_values[engineer_id]
            active_cards = assigned_by_engineer.get(engineer_id, [])
            cancelled_cards = cancelled_by_engineer.get(engineer_id, [])
            schedule = schedule_by_engineer.get(engineer_id, {})
            columns.append(
                {
                    "engineer_id": engineer_id,
                    "name": engineer.get("name") or "Имя инженера недоступно",
                    "transport_type": engineer.get("transport_type") or "NONE",
                    "start_address": engineer.get("start_address"),
                    "start_coordinate": _coordinate(
                        engineer,
                        latitude_key="start_latitude",
                        longitude_key="start_longitude",
                    ),
                    "shift_start": schedule.get("shift_start"),
                    "shift_end": schedule.get("shift_end"),
                    "unavailable": not bool(engineer.get("active", True))
                    or (bool(active_cards) and engineer_id not in current_schedule_ids),
                    "active_count": len(active_cards),
                    "route_duration_min": sum(
                        int(card.get("duration_min") or 0)
                        + int(card.get("travel_from_previous_min") or 0)
                        for card in active_cards
                    ),
                    "distance_meters": sum(
                        int(card.get("distance_from_previous_meters") or 0)
                        for card in active_cards
                    ),
                    "jobs": active_cards,
                    "cancelled_jobs": cancelled_cards,
                }
            )
        columns.sort(key=lambda item: (not bool(item["active_count"]), item["name"]))
        moved = []
        horizon = []
        for item in unassigned_rows:
            job_id = int(item["job_id"])
            if any(
                card["job_id"] == job_id
                for cards in assigned_by_engineer.values()
                for card in cards
            ):
                continue
            final_horizon = bool(item.get("_final_horizon_outcome"))
            reason_snapshot = (
                version_batch_snapshot if final_horizon else batch_snapshot
            )
            job = job_snapshot(job_id, final_horizon=final_horizon)
            statuses.add(str(job.get("status") or "NEW"))
            later = later_dates.get(job_id)
            diagnostic_flags = dict(item.get("diagnostic_flags") or {})
            saved_reason_code = str(item.get("primary_reason_code") or "")
            required_equipment = job.get("required_equipment", [])
            equipment_units = reason_snapshot.get("equipment_units", {})
            missing_equipment = []
            for equipment in required_equipment:
                equipment_id = int(
                    equipment.get("id") if isinstance(equipment, dict) else equipment
                )
                if "equipment_units" not in reason_snapshot:
                    continue
                available_units = int(
                    equipment_units.get(
                        str(equipment_id), equipment_units.get(equipment_id, 0)
                    )
                    or 0
                )
                if available_units <= 0:
                    missing_equipment.append(
                        {
                            "id": equipment_id,
                            "name": (
                                equipment.get("name")
                                if isinstance(equipment, dict)
                                else database_equipment_names.get(equipment_id)
                            ),
                            "available_units": available_units,
                        }
                    )
            if missing_equipment:
                diagnostic_flags["missing_equipment"] = missing_equipment
                diagnostic_flags.setdefault(
                    "required_equipment_type_ids",
                    [value["id"] for value in missing_equipment],
                )
                # Repair legacy/generic daily reasons using the immutable batch
                # snapshot. Zero mandatory stock is more specific than a missing
                # current shift and makes the job impossible throughout the run.
                if saved_reason_code in {
                    "",
                    "NO_AVAILABLE_ENGINEER",
                    "NO_AVAILABLE_ENGINEER_TODAY",
                    "NO_COMPATIBLE_ENGINEER",
                    "NO_COMPATIBLE_ENGINEER_IN_HORIZON",
                    "NO_SHIFT_IN_HORIZON",
                    "NOT_SELECTED_BY_OPTIMIZER",
                    "NOT_ASSIGNED_WITHIN_HORIZON",
                    "DATASET_LIMIT",
                    "DAILY_EQUIPMENT_CAPACITY",
                    "EQUIPMENT_UNAVAILABLE_IN_HORIZON",
                }:
                    saved_reason_code = "EQUIPMENT_UNAVAILABLE_IN_HORIZON"
            if saved_reason_code in {
                "NO_AVAILABLE_ENGINEER",
                "NO_AVAILABLE_ENGINEER_TODAY",
                "NO_COMPATIBLE_ENGINEER",
                "NO_COMPATIBLE_ENGINEER_IN_HORIZON",
            }:
                diagnostic_flags.setdefault(
                    "required_qualification_ids",
                    job.get("required_qualifications", []),
                )
                diagnostic_flags.setdefault(
                    "required_equipment_type_ids",
                    [value["id"] for value in job.get("required_equipment", [])],
                )
                diagnostic_flags.setdefault(
                    "required_transport", job.get("required_transport")
                )
            if saved_reason_code in {"NO_SHIFT_IN_HORIZON", "NO_AVAILABLE_ENGINEER"}:
                diagnostic_flags.setdefault("planning_date", planning_date.isoformat())
            if saved_reason_code in {
                "INVALID_TIME_WINDOW",
                "INVALID_TIME_WINDOW_FOR_HORIZON",
                "DAILY_TIME_WINDOW_CONFLICT",
            }:
                diagnostic_flags.setdefault(
                    "window_start_min", job.get("window_start_min")
                )
                diagnostic_flags.setdefault("window_end_min", job.get("window_end_min"))
            if saved_reason_code == "DURATION_EXCEEDS_ALL_SHIFTS":
                diagnostic_flags.setdefault("required_minutes", job.get("duration_min"))
            if saved_reason_code in {
                "NOT_SELECTED_BY_OPTIMIZER",
                "DATASET_LIMIT",
            }:
                diagnostic_flags.setdefault("final_penalty", item.get("drop_penalty"))
            if run is not None and run.solver_status == "FEASIBLE_TIME_LIMIT":
                diagnostic_flags.setdefault("solver_status", run.solver_status)
                diagnostic_flags.setdefault("solver_time_ms", run.solver_time_ms)
            if job_id in final_unassigned_ids and later is None:
                diagnostic_flags.setdefault(
                    "horizon_end",
                    version_batch_snapshot.get("project", {}).get(
                        "maximum_horizon_end"
                    ),
                )
            primary = unassigned_reason(
                saved_reason_code,
                solver_status=run.solver_status if run else None,
                diagnostic_flags=diagnostic_flags,
                final_horizon=job_id in final_unassigned_ids and later is None,
            )
            card = {
                "job_id": job_id,
                "outcome": "UNASSIGNED_TODAY",
                "status": job.get("status", "NEW"),
                "priority": job.get("priority", "LOW"),
                "address": job.get("address"),
                "work_type": job.get("work_type_name"),
                "duration_min": job.get("duration_min"),
                "sla_date": job.get("sla_date"),
                "overdue": _is_overdue(job.get("sla_date"), planning_date),
                "created_at": job.get("created_at") or job.get("job_created_at"),
                "required_equipment": job.get("required_equipment", []),
                "coordinate": _coordinate(job),
                "current_data_changed": job.get("current_data_changed", False),
                "later_assignment_date": later,
                "final_horizon_outcome": later is None
                and job_id in final_unassigned_ids,
                "primary_reason": primary,
            }
            (moved if later is not None else horizon).append(card)
        if run is not None:
            input_ids = set(run_jobs)
            assigned_ids = {
                card["job_id"]
                for cards in assigned_by_engineer.values()
                for card in cards
            }
            unassigned_ids = {card["job_id"] for card in [*moved, *horizon]}
            missing_ids = input_ids - assigned_ids - unassigned_ids
            if missing_ids or len(input_ids) != int(run.input_jobs_count or 0):
                logger.error(
                    "planning_day_outcome_invariant_failed project_id=%s run_id=%s "
                    "input_count=%s snapshot_count=%s missing_job_ids=%s",
                    project_id,
                    run.id,
                    run.input_jobs_count,
                    len(input_ids),
                    sorted(missing_ids),
                )
            for job_id in sorted(missing_ids):
                job = job_snapshot(job_id)
                statuses.add(str(job.get("status") or "NEW"))
                horizon.append(
                    {
                        "job_id": job_id,
                        "outcome": "UNASSIGNED_TODAY",
                        "status": job.get("status", "NEW"),
                        "priority": job.get("priority", "LOW"),
                        "address": job.get("address"),
                        "work_type": job.get("work_type_name"),
                        "duration_min": job.get("duration_min"),
                        "sla_date": job.get("sla_date"),
                        "overdue": _is_overdue(job.get("sla_date"), planning_date),
                        "created_at": job.get("created_at"),
                        "required_equipment": job.get("required_equipment", []),
                        "coordinate": _coordinate(job),
                        "current_data_changed": job.get("current_data_changed", False),
                        "later_assignment_date": None,
                        "final_horizon_outcome": False,
                        "primary_reason": reason("RESULT_DATA_UNAVAILABLE"),
                    }
                )
        moved.sort(key=lambda item: unassigned_sort_key(item, planning_date))
        horizon.sort(key=lambda item: unassigned_sort_key(item, planning_date))
        result_available = bool(
            day_result is not None
            or run is not None
            or assignment_rows
            or cancelled_snapshots
            or unassigned_rows
        )
        return {
            **empty,
            "day_result_id": (
                int(day_result.id)
                if day_result is not None
                else int(run.id)
                if run is not None
                else None
            ),
            "result_available": result_available,
            "solver_status": run.solver_status if run is not None else None,
            "counts": {
                "assigned": sum(len(value) for value in assigned_by_engineer.values()),
                "unassigned": len(moved) + len(horizon),
                "cancelled": sum(
                    len(value) for value in cancelled_by_engineer.values()
                ),
            },
            "engineer_columns": columns,
            "unassigned": {"moved": moved, "horizon": horizon},
            "available_filters": {
                **empty["available_filters"],
                "statuses": sorted(statuses),
            },
        }

    async def _planning_source_contexts(
        self,
        *,
        project_id: int,
        target_version_number: int,
        planning_date: date,
        job_ids: set[int],
    ) -> dict[int, dict[str, Any]]:
        if not job_ids:
            return {}
        rows = (
            (
                await self._session.execute(
                    select(
                        planning_runs,
                        planning_batches.c.input_snapshot.label("batch_input_snapshot"),
                        project_plan_versions.c.version_number.label(
                            "source_version_number"
                        ),
                    )
                    .join(
                        planning_batches,
                        planning_batches.c.id == planning_runs.c.planning_batch_id,
                    )
                    .join(
                        project_plan_versions,
                        project_plan_versions.c.planning_batch_id
                        == planning_runs.c.planning_batch_id,
                    )
                    .where(
                        planning_runs.c.project_id == project_id,
                        planning_runs.c.planning_date == planning_date,
                        planning_runs.c.status == "SUCCESS",
                        project_plan_versions.c.project_id == project_id,
                        project_plan_versions.c.version_number <= target_version_number,
                    )
                    .order_by(
                        project_plan_versions.c.version_number.desc(),
                        planning_runs.c.id.desc(),
                    )
                )
            )
            .mappings()
            .all()
        )
        contexts: dict[int, dict[str, Any]] = {}
        seen_run_ids: set[int] = set()
        for row in rows:
            run_id = int(row.id)
            if run_id in seen_run_ids:
                continue
            seen_run_ids.add(run_id)
            run_snapshot = row.input_snapshot or {}
            batch_value = row.batch_input_snapshot or {}
            available_ids = {
                int(item["id"])
                for item in [
                    *batch_value.get("jobs", []),
                    *run_snapshot.get("jobs", []),
                ]
            }
            for job_id in job_ids & available_ids:
                contexts.setdefault(
                    job_id,
                    {
                        "run": row,
                        "run_snapshot": run_snapshot,
                        "batch_snapshot": batch_value,
                    },
                )
            if len(contexts) == len(job_ids):
                break
        return contexts

    async def get_planning_job_explanation(
        self, project_id: int, day_result_id: int, job_id: int
    ) -> dict[str, Any] | None:
        started = monotonic()
        saved_day = (
            (
                await self._session.execute(
                    select(planning_day_results)
                    .join(
                        project_plan_versions,
                        project_plan_versions.c.id
                        == planning_day_results.c.plan_version_id,
                    )
                    .where(
                        planning_day_results.c.id == day_result_id,
                        planning_day_results.c.project_id == project_id,
                        project_plan_versions.c.is_current.is_(True),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        run = (
            (
                await self._session.execute(
                    select(planning_runs).where(
                        planning_runs.c.id
                        == (
                            saved_day.planning_run_id
                            if saved_day is not None
                            else day_result_id
                        ),
                        planning_runs.c.project_id == project_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        planning_date = (
            saved_day.planning_date
            if saved_day is not None
            else run.planning_date
            if run is not None
            else None
        )
        if planning_date is None:
            planning_date = await self._session.scalar(
                select(project_plan_assignments.c.planning_date)
                .join(
                    project_plan_versions,
                    project_plan_versions.c.id
                    == project_plan_assignments.c.plan_version_id,
                )
                .where(
                    project_plan_versions.c.project_id == project_id,
                    project_plan_versions.c.is_current.is_(True),
                    project_plan_assignments.c.job_id == job_id,
                )
                .limit(1)
            )
        if planning_date is None:
            logger.warning(
                "planning_job_details_response project_id=%s day_result_id=%s "
                "job_id=%s status=NOT_FOUND duration_ms=%s",
                project_id,
                day_result_id,
                job_id,
                round((monotonic() - started) * 1000),
            )
            return None
        day = await self.get_planning_board_day(project_id, planning_date, None)
        # The explanation belongs to the immutable result requested by the
        # client, never to another (for example, older) run for the same date.
        if day.get("day_result_id") != day_result_id:
            logger.warning(
                "planning_job_details_response project_id=%s day_result_id=%s "
                "job_id=%s status=VERSION_CHANGED duration_ms=%s",
                project_id,
                day_result_id,
                job_id,
                round((monotonic() - started) * 1000),
            )
            return None
        card = next(
            (
                item
                for column in day.get("engineer_columns", [])
                for item in [*column["jobs"], *column["cancelled_jobs"]]
                if item["job_id"] == job_id
            ),
            None,
        )
        if card is None:
            card = next(
                (
                    item
                    for group in day.get("unassigned", {}).values()
                    for item in group
                    if item["job_id"] == job_id
                ),
                None,
            )
        if card is None:
            logger.warning(
                "planning_job_details_response project_id=%s day_result_id=%s "
                "job_id=%s status=NOT_FOUND duration_ms=%s",
                project_id,
                day_result_id,
                job_id,
                round((monotonic() - started) * 1000),
            )
            return None
        assignment = next(
            (
                {
                    "engineer_id": column["engineer_id"],
                    "engineer_name": column["name"],
                    "position": card.get("route_position"),
                    "planned_start": card.get("planned_start"),
                    "planned_end": card.get("planned_end"),
                }
                for column in day.get("engineer_columns", [])
                if card in [*column["jobs"], *column["cancelled_jobs"]]
            ),
            None,
        )
        if assignment is not None and card["outcome"] == "ASSIGNED":
            target_version_number = await self._session.scalar(
                select(project_plan_versions.c.version_number).where(
                    project_plan_versions.c.id == day.get("plan_version_id"),
                    project_plan_versions.c.project_id == project_id,
                )
            )
            if target_version_number is not None:
                source_context = (
                    await self._planning_source_contexts(
                        project_id=project_id,
                        target_version_number=int(target_version_number),
                        planning_date=planning_date,
                        job_ids={job_id},
                    )
                ).get(job_id)
                if source_context is not None:
                    run = source_context["run"]
        input_snapshot = (run.input_snapshot or {}) if run is not None else {}
        input_jobs = {int(item["id"]): item for item in input_snapshot.get("jobs", [])}
        input_engineers = input_snapshot.get("engineers", [])
        input_job = input_jobs.get(job_id, {})
        eligible_count = int(
            input_snapshot.get("eligible_engineer_counts", {}).get(
                str(job_id),
                _eligible_engineer_count(input_job, input_engineers),
            )
        )
        eligibility = []
        if assignment is not None and card["outcome"] == "ASSIGNED" and input_job:
            if input_job.get("required_qualifications"):
                eligibility.append(
                    {
                        "code": "QUALIFICATIONS",
                        "text": "Требуемые квалификации подтверждены результатом",
                        "parameters": {
                            "qualification_ids": input_job["required_qualifications"]
                        },
                    }
                )
            if input_job.get("required_equipment"):
                eligibility.append(
                    {
                        "code": "EQUIPMENT",
                        "text": "Требуемое оборудование учтено в назначении",
                        "parameters": {
                            "equipment_type_ids": input_job["required_equipment"]
                        },
                    }
                )
            if input_job.get("required_transport"):
                eligibility.append(
                    {
                        "code": "TRANSPORT",
                        "text": "Транспорт соответствует сохранённому требованию",
                        "parameters": {
                            "required_transport": input_job["required_transport"]
                        },
                    }
                )
            eligibility.extend(
                [
                    {
                        "code": "SHIFT",
                        "text": "Назначение прошло проверку границ смены",
                        "parameters": {},
                    },
                    {
                        "code": "TIME_WINDOW",
                        "text": "Плановое время прошло проверку временного окна",
                        "parameters": {
                            "window_start_min": input_job.get("window_start_min"),
                            "window_end_min": input_job.get("window_end_min"),
                        },
                    },
                ]
            )
        penalty_components = input_snapshot.get("penalty_components", {}).get(
            str(job_id), {}
        )
        priority_factors = []
        if card.get("priority", "LOW") != "LOW":
            priority_factors.append(
                reason(
                    "WORK_TYPE_PRIORITY",
                    {
                        "priority": card.get("priority"),
                        "priority_bonus": penalty_components.get("priority_bonus"),
                    },
                )
            )
        if card.get("overdue"):
            sla_date = _as_date(card["sla_date"])
            priority_factors.append(
                reason(
                    "OVERDUE_PRIORITY",
                    {"overdue_days": (planning_date - sla_date).days},
                )
            )
        elif str(card.get("sla_date"))[:10] == planning_date.isoformat():
            priority_factors.append(
                reason("SLA_DUE_TODAY", {"sla_date": planning_date.isoformat()})
            )
        result = {
            "day_result_id": day_result_id,
            "planning_date": planning_date,
            "job": card,
            "outcome": card["outcome"],
            "assignment": assignment,
            "eligibility": eligibility,
            "eligible_engineers_count": eligible_count,
            "priority_factors": priority_factors,
            "route_factors": {
                "travel_from_previous_min": card.get("travel_from_previous_min"),
                "distance_from_previous_meters": card.get(
                    "distance_from_previous_meters"
                ),
            },
            "outcome_reasons": [card["primary_reason"]],
            "result_text": card["primary_reason"]["text"],
            "technical": {
                "solver_status": day.get("solver_status"),
                "objective": run.objective if run is not None else None,
                "drop_cost": run.drop_cost if run is not None else None,
                "travel_cost": run.travel_cost if run is not None else None,
                "objective_components": (
                    run.objective_range_snapshot if run is not None else {}
                ),
            },
        }
        logger.info(
            "planning_job_details_response project_id=%s day_result_id=%s "
            "job_id=%s status=SUCCESS duration_ms=%s",
            project_id,
            day_result_id,
            job_id,
            round((monotonic() - started) * 1000),
        )
        return result

    async def list_plan_versions(
        self, project_id: int, limit: int, offset: int
    ) -> dict[str, Any]:
        total = int(
            await self._session.scalar(
                select(func.count(project_plan_versions.c.id)).where(
                    project_plan_versions.c.project_id == project_id
                )
            )
            or 0
        )
        rows = (
            (
                await self._session.execute(
                    select(project_plan_versions)
                    .where(project_plan_versions.c.project_id == project_id)
                    .order_by(project_plan_versions.c.version_number.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .mappings()
            .all()
        )
        return {
            "items": [_jsonable(dict(row)) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def get_plan_version(
        self, project_id: int, version_id: int
    ) -> dict[str, Any] | None:
        version = (
            (
                await self._session.execute(
                    select(project_plan_versions).where(
                        project_plan_versions.c.project_id == project_id,
                        project_plan_versions.c.id == version_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if version is None else await self._plan_version_result(version)

    async def _plan_version_result(self, version: Any) -> dict[str, Any]:
        timezone_name = await self._session.scalar(
            select(projects.c.planning_timezone).where(
                projects.c.id == version.project_id
            )
        )
        comparison_snapshot = (
            await self._session.scalar(
                select(planning_batches.c.input_snapshot).where(
                    planning_batches.c.id == version.planning_batch_id
                )
            )
            if version.planning_batch_id is not None
            else {}
        ) or {}
        previous_version = (
            (
                await self._session.execute(
                    select(project_plan_versions)
                    .where(
                        project_plan_versions.c.project_id == version.project_id,
                        project_plan_versions.c.version_number < version.version_number,
                    )
                    .order_by(project_plan_versions.c.version_number.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        previous_comparison_snapshot = (
            await self._session.scalar(
                select(planning_batches.c.input_snapshot).where(
                    planning_batches.c.id == previous_version.planning_batch_id
                )
            )
            if previous_version is not None
            and previous_version.planning_batch_id is not None
            else {}
        ) or {}
        trigger_rows = (
            (
                await self._session.execute(
                    select(
                        planning_events.c.id,
                        planning_events.c.event_type,
                        planning_events.c.job_ids,
                        planning_events.c.engineer_ids,
                        planning_events.c.initiator,
                        planning_events.c.requested_at,
                    )
                    .where(
                        planning_events.c.project_id == version.project_id,
                        planning_events.c.published_plan_version_id == version.id,
                    )
                    .order_by(planning_events.c.requested_at, planning_events.c.id)
                )
            )
            .mappings()
            .all()
        )
        if not trigger_rows and version.planning_event_id is not None:
            trigger_rows = (
                (
                    await self._session.execute(
                        select(
                            planning_events.c.id,
                            planning_events.c.event_type,
                            planning_events.c.job_ids,
                            planning_events.c.engineer_ids,
                            planning_events.c.initiator,
                            planning_events.c.requested_at,
                        ).where(planning_events.c.id == version.planning_event_id)
                    )
                )
                .mappings()
                .all()
            )
        trigger_engineer_ids = {
            int(engineer_id)
            for row in trigger_rows
            for engineer_id in (row.engineer_ids or [])
        }
        trigger_engineer_names: dict[int, str] = {}
        if trigger_engineer_ids:
            trigger_engineer_names = {
                int(row.id): str(row.name)
                for row in (
                    (
                        await self._session.execute(
                            select(engineers.c.id, engineers.c.name).where(
                                engineers.c.project_id == version.project_id,
                                engineers.c.id.in_(trigger_engineer_ids),
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            }
        serialized_triggers = []
        for row in trigger_rows:
            item = _jsonable(dict(row))
            item["engineers"] = [
                {
                    "engineer_id": int(engineer_id),
                    "engineer_name": trigger_engineer_names.get(
                        int(engineer_id), f"Инженер #{engineer_id}"
                    ),
                }
                for engineer_id in (row.engineer_ids or [])
            ]
            serialized_triggers.append(item)
        rows = (
            (
                await self._session.execute(
                    select(
                        project_plan_assignments,
                        jobs.c.address,
                        jobs.c.status,
                        work_types.c.name.label("work_type"),
                        work_types.c.priority.label("priority"),
                        jobs.c.latitude,
                        jobs.c.longitude,
                        engineers.c.name.label("engineer_name"),
                        engineers.c.transport_type,
                        engineers.c.start_address.label("engineer_start_address"),
                        engineers.c.start_latitude,
                        engineers.c.start_longitude,
                    )
                    .join(jobs, jobs.c.id == project_plan_assignments.c.job_id)
                    .join(work_types, work_types.c.id == jobs.c.work_type_id)
                    .join(
                        engineers,
                        engineers.c.id == project_plan_assignments.c.engineer_id,
                    )
                    .where(project_plan_assignments.c.plan_version_id == version.id)
                    .order_by(
                        project_plan_assignments.c.planning_date,
                        project_plan_assignments.c.engineer_id,
                        project_plan_assignments.c.sequence,
                    )
                )
            )
            .mappings()
            .all()
        )
        previous_rows = []
        if previous_version is not None:
            previous_rows = (
                (
                    await self._session.execute(
                        select(
                            project_plan_assignments,
                            engineers.c.name.label("engineer_name"),
                        )
                        .join(
                            engineers,
                            engineers.c.id == project_plan_assignments.c.engineer_id,
                        )
                        .where(
                            project_plan_assignments.c.plan_version_id
                            == previous_version.id
                        )
                        .order_by(
                            project_plan_assignments.c.planning_date,
                            project_plan_assignments.c.engineer_id,
                            project_plan_assignments.c.sequence,
                        )
                    )
                )
                .mappings()
                .all()
            )
        metric_version_ids = [int(version.id)]
        if previous_version is not None:
            metric_version_ids.append(int(previous_version.id))
        metric_day_rows = (
            (
                await self._session.execute(
                    select(
                        planning_day_results.c.plan_version_id,
                        planning_day_results.c.planning_date,
                    ).where(
                        planning_day_results.c.plan_version_id.in_(metric_version_ids)
                    )
                )
            )
            .mappings()
            .all()
        )
        metric_dates_by_version: dict[int, list[date]] = {}
        for row in metric_day_rows:
            metric_dates_by_version.setdefault(int(row.plan_version_id), []).append(
                _as_date(row.planning_date)
            )
        input_changes = _planning_input_changes(
            previous_comparison_snapshot, comparison_snapshot
        )
        input_changes = _classify_carried_forward_jobs(
            input_changes, rows, comparison_snapshot
        )
        changes = (
            (
                await self._session.execute(
                    select(plan_changes)
                    .where(plan_changes.c.plan_version_id == version.id)
                    .order_by(plan_changes.c.id)
                )
            )
            .mappings()
            .all()
        )
        changed_job_ids = {int(row.job_id) for row in changes}
        unassigned_by_job = {
            int(item["job_id"]): item
            for item in (version.unassigned_jobs or [])
            if item.get("job_id") is not None
        }
        previous_unassigned_by_job = {
            int(item["job_id"]): item
            for item in (
                previous_version.unassigned_jobs or []
                if previous_version is not None
                else []
            )
            if item.get("job_id") is not None
        }
        unassigned_changed_ids: list[int] = []
        removed_unassigned_ids: list[int] = []
        unchanged_unassigned_ids: list[int] = []
        if previous_version is not None:
            for job_id, current_unassigned in unassigned_by_job.items():
                if job_id in changed_job_ids:
                    continue
                previous_unassigned = previous_unassigned_by_job.get(job_id)
                if previous_unassigned is None or not _same_unassigned_reason(
                    previous_unassigned, current_unassigned
                ):
                    unassigned_changed_ids.append(job_id)
                else:
                    unchanged_unassigned_ids.append(job_id)
            removed_unassigned_ids = [
                job_id
                for job_id in previous_unassigned_by_job
                if job_id not in unassigned_by_job and job_id not in changed_job_ids
            ]
        comparison_job_ids = (
            changed_job_ids
            | set(unassigned_changed_ids)
            | set(unchanged_unassigned_ids)
            | set(removed_unassigned_ids)
        )
        change_jobs: dict[int, dict[str, Any]] = {}
        if comparison_job_ids:
            change_job_rows = (
                (
                    await self._session.execute(
                        select(
                            jobs.c.id,
                            jobs.c.address,
                            jobs.c.status,
                            work_types.c.name.label("work_type"),
                        )
                        .join(work_types, work_types.c.id == jobs.c.work_type_id)
                        .where(
                            jobs.c.project_id == version.project_id,
                            jobs.c.id.in_(comparison_job_ids),
                        )
                    )
                )
                .mappings()
                .all()
            )
            change_jobs = {int(row.id): _jsonable(dict(row)) for row in change_job_rows}
        change_engineer_ids = {
            int(assignment["engineer_id"])
            for row in changes
            for assignment in (row.old_assignment, row.new_assignment)
            if assignment and assignment.get("engineer_id") is not None
        }
        change_engineer_names: dict[int, str] = {}
        if change_engineer_ids:
            change_engineer_names = {
                int(row.id): str(row.name)
                for row in (
                    (
                        await self._session.execute(
                            select(engineers.c.id, engineers.c.name).where(
                                engineers.c.project_id == version.project_id,
                                engineers.c.id.in_(change_engineer_ids),
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            }
        serialized_changes = []
        for row in changes:
            item = _jsonable(dict(row))
            if item["change_type"] == "CHANGED" and _same_assignment(
                item.get("old_assignment"), item.get("new_assignment")
            ):
                # Versions published before assignment values were normalized may
                # contain a false diff (JSON date string versus Python date).
                continue
            if item["change_type"] == "DISPLACED":
                final_unassigned = unassigned_by_job.get(int(item["job_id"]))
                final_reason = (
                    final_unassigned.get("primary_reason_code")
                    if final_unassigned
                    else None
                )
                item["reason"] = _resolved_displaced_reason(
                    item.get("reason"), final_reason
                )
                item["reason_detail"] = _comparison_reason_detail(
                    item["reason"],
                    final_unassigned,
                    int(item["job_id"]),
                    comparison_snapshot,
                )
            item["job"] = change_jobs.get(int(item["job_id"]), {})
            for assignment_key in ("old_assignment", "new_assignment"):
                assignment = item.get(assignment_key)
                if assignment and assignment.get("engineer_id") is not None:
                    engineer_id = int(assignment["engineer_id"])
                    assignment["engineer_name"] = change_engineer_names.get(
                        engineer_id, f"Инженер #{engineer_id}"
                    )
            serialized_changes.append(item)
        newly_assigned_routes: dict[tuple[str, int], list[int]] = {}
        for item in serialized_changes:
            assignment = item.get("new_assignment")
            if item.get("change_type") != "ASSIGNED" or not assignment:
                continue
            route_key = (
                str(assignment.get("planning_date")),
                int(assignment["engineer_id"]),
            )
            newly_assigned_routes.setdefault(route_key, []).append(int(item["job_id"]))
        for item in serialized_changes:
            assignment = item.get("new_assignment")
            related_job_ids: list[int] = []
            if assignment:
                route_key = (
                    str(assignment.get("planning_date")),
                    int(assignment["engineer_id"]),
                )
                related_job_ids = [
                    job_id
                    for job_id in newly_assigned_routes.get(route_key, [])
                    if job_id != int(item["job_id"])
                ]
            previous_unassigned = previous_unassigned_by_job.get(int(item["job_id"]))
            item["explanation_context"] = {
                "new_input_job": int(item["job_id"])
                in set(input_changes["new_job_ids"]),
                "related_assignment_job_ids": related_job_ids,
                "previous_unassigned_reason": (
                    _comparison_reason_detail(
                        str(previous_unassigned.get("primary_reason_code") or ""),
                        previous_unassigned,
                        int(item["job_id"]),
                        previous_comparison_snapshot,
                    )
                    if previous_unassigned is not None
                    else None
                ),
            }
        serialized_changed_job_ids = {
            int(item["job_id"]) for item in serialized_changes
        }
        unchanged_assignment_count = sum(
            int(row.job_id) not in serialized_changed_job_ids for row in rows
        )
        unassigned_changes = []
        for job_id in unassigned_changed_ids:
            previous_unassigned = previous_unassigned_by_job.get(job_id)
            current_unassigned = unassigned_by_job[job_id]
            unassigned_changes.append(
                {
                    "job_id": job_id,
                    "change_type": (
                        "UNASSIGNED_REASON_CHANGED"
                        if previous_unassigned is not None
                        else "NEW_UNASSIGNED"
                    ),
                    "job": change_jobs.get(job_id, {}),
                    "previous_reason": (
                        _comparison_reason_detail(
                            str(previous_unassigned.get("primary_reason_code") or ""),
                            previous_unassigned,
                            job_id,
                            previous_comparison_snapshot,
                        )
                        if previous_unassigned is not None
                        else None
                    ),
                    "current_reason": _comparison_reason_detail(
                        str(current_unassigned.get("primary_reason_code") or ""),
                        current_unassigned,
                        job_id,
                        comparison_snapshot,
                    ),
                }
            )
        for job_id in removed_unassigned_ids:
            previous_unassigned = previous_unassigned_by_job[job_id]
            unassigned_changes.append(
                {
                    "job_id": job_id,
                    "change_type": "UNASSIGNED_REMOVED",
                    "job": change_jobs.get(job_id, {}),
                    "previous_reason": _comparison_reason_detail(
                        str(previous_unassigned.get("primary_reason_code") or ""),
                        previous_unassigned,
                        job_id,
                        previous_comparison_snapshot,
                    ),
                    "current_reason": None,
                }
            )
        unchanged_unassigned = [
            {
                "job_id": job_id,
                "job": change_jobs.get(job_id, {}),
                "reason": _comparison_reason_detail(
                    str(unassigned_by_job[job_id].get("primary_reason_code") or ""),
                    unassigned_by_job[job_id],
                    job_id,
                    comparison_snapshot,
                ),
            }
            for job_id in unchanged_unassigned_ids
        ]
        return {
            "version": _jsonable(dict(version)),
            "timezone": str(timezone_name),
            "assignments": [_jsonable(dict(row)) for row in rows],
            "changes": serialized_changes,
            "comparison": {
                "previous_version_number": (
                    int(previous_version.version_number)
                    if previous_version is not None
                    else None
                ),
                "changed_count": len(serialized_changes) + len(unassigned_changes),
                "unchanged_count": (
                    unchanged_assignment_count + len(unchanged_unassigned)
                ),
                "unassigned_changes": unassigned_changes,
                "unchanged_unassigned": unchanged_unassigned,
                "triggers": serialized_triggers,
                "input_changes": input_changes,
                "metrics": {
                    "previous": (
                        _planning_metrics(
                            previous_rows,
                            metric_dates_by_version.get(int(previous_version.id), []),
                        )
                        if previous_version is not None
                        else None
                    ),
                    "current": _planning_metrics(
                        rows, metric_dates_by_version.get(int(version.id), [])
                    ),
                },
            },
            "route_metrics": _route_metrics(rows),
        }

    async def _current_today(
        self, version: Any | None, planning_date: date
    ) -> list[dict[str, Any]]:
        if version is None:
            return []
        actual_started_at = (
            select(func.max(job_status_history.c.created_at))
            .where(
                job_status_history.c.job_id == jobs.c.id,
                job_status_history.c.new_status == "IN_PROGRESS",
            )
            .correlate(jobs)
            .scalar_subquery()
        )
        actual_completed_at = (
            select(func.max(job_status_history.c.created_at))
            .where(
                job_status_history.c.job_id == jobs.c.id,
                job_status_history.c.new_status == "COMPLETED",
            )
            .correlate(jobs)
            .scalar_subquery()
        )
        rows = (
            (
                await self._session.execute(
                    select(
                        project_plan_assignments,
                        jobs.c.status,
                        work_types.c.priority.label("priority"),
                        jobs.c.previous_status,
                        jobs.c.address,
                        jobs.c.latitude,
                        jobs.c.longitude,
                        jobs.c.sla_date,
                        jobs.c.work_type_id,
                        actual_started_at.label("actual_started_at"),
                        actual_completed_at.label("actual_completed_at"),
                    )
                    .join(jobs, jobs.c.id == project_plan_assignments.c.job_id)
                    .join(work_types, work_types.c.id == jobs.c.work_type_id)
                    .where(
                        project_plan_assignments.c.plan_version_id == version.id,
                        project_plan_assignments.c.planning_date == planning_date,
                    )
                    .order_by(
                        project_plan_assignments.c.engineer_id,
                        project_plan_assignments.c.sequence,
                    )
                )
            )
            .mappings()
            .all()
        )
        return [_jsonable(dict(row)) for row in rows]

    @staticmethod
    def _fingerprint(version: Any | None, assignments: list[dict[str, Any]]) -> str:
        payload = {
            "version_id": int(version.id) if version is not None else None,
            "assignments": [
                {
                    "job_id": item["job_id"],
                    "engineer_id": item["engineer_id"],
                    "sequence": item["sequence"],
                    "planned_start": item["planned_start"],
                    "planned_finish": item["planned_finish"],
                    "status": item["status"],
                    "actual_started_at": item.get("actual_started_at"),
                    "actual_completed_at": item.get("actual_completed_at"),
                }
                for item in assignments
            ],
        }
        return hashlib.sha256(
            json.dumps(
                _jsonable(payload), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()


def _normalize_assignments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        result.append(
            {
                "job_id": int(row["job_id"]),
                "planning_date": _as_date(row["planning_date"]),
                "engineer_id": int(row["engineer_id"]),
                "sequence": int(row["sequence"]),
                "planned_arrival": _as_datetime(
                    row.get("planned_arrival") or row["planned_start"]
                ),
                "planned_start": _as_datetime(row["planned_start"]),
                "planned_finish": _as_datetime(row["planned_finish"]),
                "travel_from_previous_min": int(
                    row.get("travel_from_previous_min") or 0
                ),
                "waiting_before_job_min": int(row.get("waiting_before_job_min") or 0),
                "distance_from_previous_meters": int(
                    row.get("distance_from_previous_meters") or 0
                ),
                "requirement_snapshot": row.get("requirement_snapshot") or {},
            }
        )
    return result


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _as_datetime(value: datetime | str) -> datetime:
    return (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _same_instant(left: datetime | str, right: datetime | str) -> bool:
    left_value = _as_datetime(left)
    right_value = _as_datetime(right)
    if left_value.tzinfo is not None:
        left_value = left_value.astimezone(timezone.utc)
    if right_value.tzinfo is not None:
        right_value = right_value.astimezone(timezone.utc)
    return left_value == right_value


def _historical_day_result_matches_assignments(
    day_result: Any,
    route_assignments: list[dict[str, Any]],
    current_assignments: dict[int, dict[str, Any]],
) -> bool:
    """Reject copied results whose assigned jobs moved or disappeared.

    An all-unassigned historical result remains useful: the board can show that
    the jobs were not assigned on that day and, when applicable, where they were
    moved later. Assigned route outcomes are only valid while the published plan
    still contains the same job on the same date and engineer.
    """
    planning_date = _as_date(day_result.planning_date)
    for source in route_assignments:
        current = current_assignments.get(int(source["job_id"]))
        if current is None:
            return False
        if _as_date(current["planning_date"]) != planning_date:
            return False
        if int(current["engineer_id"]) != int(source["engineer_id"]):
            return False
    return True


def _include_final_unassigned_rows(
    daily_rows: list[Any], final_values: list[dict[str, Any]]
) -> list[Any]:
    """Merge the authoritative final horizon outcome into the selected day."""
    result = [dict(item) for item in daily_rows]
    by_job = {int(item["job_id"]): item for item in result}
    for value in final_values:
        if value.get("job_id") is None:
            continue
        job_id = int(value["job_id"])
        if job_id in by_job:
            current = by_job[job_id]
            if value.get("primary_reason_code"):
                current["primary_reason_code"] = value["primary_reason_code"]
            current["diagnostic_flags"] = {
                **(current.get("diagnostic_flags") or {}),
                **(value.get("diagnostic_flags") or {}),
            }
            if value.get("drop_penalty") is not None:
                current["drop_penalty"] = value["drop_penalty"]
            current["_final_horizon_outcome"] = True
            continue
        item = {
            "job_id": job_id,
            "primary_reason_code": value.get("primary_reason_code"),
            "diagnostic_flags": value.get("diagnostic_flags") or {},
            "drop_penalty": value.get("drop_penalty"),
            "_final_horizon_outcome": True,
        }
        result.append(item)
        by_job[job_id] = item
    return result


def _snapshot_requirement_ids(
    job: dict[str, Any], snapshot: dict[str, Any], field: str
) -> list[int]:
    values = job.get(field)
    if values is None:
        work_type_id = job.get("work_type_id")
        requirements = snapshot.get(field, {})
        values = requirements.get(str(work_type_id), requirements.get(work_type_id, []))
    return sorted(
        int(value.get("id") if isinstance(value, dict) else value)
        for value in (values or [])
    )


def _resolved_displaced_reason(
    saved_reason: str | None, final_unassigned_reason: str | None
) -> str:
    """Backfill old generic diffs without erasing event-specific causes."""
    if final_unassigned_reason and saved_reason in {
        None,
        "",
        "NOT_ASSIGNED_IN_NEW_HORIZON",
    }:
        return str(final_unassigned_reason)
    return str(saved_reason or final_unassigned_reason or "NOT_ASSIGNED_IN_NEW_HORIZON")


def _same_unassigned_reason(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    previous_code = str(
        previous.get("primary_reason_code") or "NOT_ASSIGNED_WITHIN_HORIZON"
    )
    current_code = str(
        current.get("primary_reason_code") or "NOT_ASSIGNED_WITHIN_HORIZON"
    )
    previous_normalized = reason(previous_code)["code"]
    current_normalized = reason(current_code)["code"]
    if previous_normalized != current_normalized:
        return False
    if previous_normalized != "NO_EQUIPMENT":
        return True

    def equipment_ids(value: dict[str, Any]) -> tuple[int, ...]:
        diagnostics = value.get("diagnostic_flags") or {}
        identifiers = {
            int(identifier)
            for identifier in (
                diagnostics.get("equipment_type_ids")
                or diagnostics.get("required_equipment_type_ids")
                or []
            )
        }
        identifiers.update(
            int(item["id"])
            for item in diagnostics.get("missing_equipment") or []
            if isinstance(item, dict) and item.get("id") is not None
        )
        return tuple(sorted(identifiers))

    previous_ids = equipment_ids(previous)
    current_ids = equipment_ids(current)
    # Old versions sometimes persisted only the public reason code. In that
    # case there is not enough evidence to claim that the reason changed.
    return not previous_ids or not current_ids or previous_ids == current_ids


def _comparison_reason_detail(
    reason_code: str,
    final_unassigned: dict[str, Any] | None,
    job_id: int,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    diagnostics = dict((final_unassigned or {}).get("diagnostic_flags") or {})
    resolved_reason_code = reason_code or "NOT_ASSIGNED_WITHIN_HORIZON"
    normalized = reason(resolved_reason_code)["code"]
    if normalized == "NO_EQUIPMENT":
        equipment_ids = diagnostics.get("equipment_type_ids") or diagnostics.get(
            "required_equipment_type_ids"
        )
        if not equipment_ids:
            snapshot_job = next(
                (
                    item
                    for item in snapshot.get("jobs", [])
                    if int(item["id"]) == job_id
                ),
                {},
            )
            equipment_ids = _snapshot_requirement_ids(
                snapshot_job, snapshot, "required_equipment"
            )
        names = {
            int(item["id"]): item.get("name")
            for item in snapshot.get("equipment_types", [])
        }
        units = snapshot.get("equipment_units", {})
        diagnostics["missing_equipment"] = [
            {
                "id": int(equipment_id),
                "name": names.get(int(equipment_id)),
                "available_units": int(
                    units.get(str(equipment_id), units.get(int(equipment_id), 0)) or 0
                ),
            }
            for equipment_id in equipment_ids or []
        ]
    return unassigned_reason(
        resolved_reason_code,
        solver_status=None,
        diagnostic_flags=diagnostics,
        final_horizon=True,
    )


def _changes(
    old: dict[int, dict[str, Any]],
    new: dict[int, dict[str, Any]],
    version_id: int,
    project_id: int,
) -> list[dict[str, Any]]:
    result = []
    old_predecessors = _assignment_predecessors(old)
    new_predecessors = _assignment_predecessors(new)
    for job_id in sorted(set(old) | set(new)):
        before = _jsonable(old.get(job_id))
        after = _jsonable(new.get(job_id))
        if before is not None:
            before["previous_job_id"] = old_predecessors.get(job_id)
        if after is not None:
            after["previous_job_id"] = new_predecessors.get(job_id)
        if before and not after:
            change_type, reason = "DISPLACED", "NOT_ASSIGNED_IN_NEW_HORIZON"
        elif after and not before:
            change_type, reason = "ASSIGNED", "NEW_ASSIGNMENT"
        else:
            if _same_assignment(before, after):
                continue
            moved_later = _as_date(after["planning_date"]) > _as_date(
                before["planning_date"]
            )
            change_type = "CHANGED"
            reason = "DISPLACED_TO_FUTURE" if moved_later else "REPLANNED"
            diagnostics = (
                (after.get("requirement_snapshot") or {}).get(
                    "protection_diagnostics", []
                )
                if after
                else []
            )
            if "CANCELLED_EN_ROUTE_ASSUMPTION" in diagnostics:
                reason = "CANCELLED_EN_ROUTE_ASSUMPTION"
        result.append(
            {
                "plan_version_id": version_id,
                "project_id": project_id,
                "job_id": job_id,
                "change_type": change_type,
                "old_assignment": _jsonable(before),
                "new_assignment": _jsonable(after),
                "reason": reason,
            }
        )
    return result


def _assignment_predecessors(
    assignments: dict[int, dict[str, Any]],
) -> dict[int, int | None]:
    routes: dict[tuple[str, int], list[tuple[int, int]]] = {}
    for job_id, assignment in assignments.items():
        route_key = (
            str(assignment.get("planning_date")),
            int(assignment["engineer_id"]),
        )
        routes.setdefault(route_key, []).append(
            (int(assignment.get("sequence") or 0), int(job_id))
        )

    result: dict[int, int | None] = {}
    for route in routes.values():
        previous_job_id: int | None = None
        for _, job_id in sorted(route):
            result[job_id] = previous_job_id
            previous_job_id = job_id
    return result


def _same_assignment(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> bool:
    if before is None or after is None:
        return before is after
    keys = (
        "planning_date",
        "engineer_id",
        "sequence",
        "previous_job_id",
        "planned_start",
        "planned_finish",
        "travel_from_previous_min",
        "waiting_before_job_min",
        "distance_from_previous_meters",
    )
    normalized_before = _jsonable(before)
    normalized_after = _jsonable(after)
    return all(normalized_before.get(key) == normalized_after.get(key) for key in keys)


def _row_field(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    mapping = getattr(row, "_mapping", row)
    getter = getattr(mapping, "get", None)
    if getter is not None:
        return getter(key, default)
    return getattr(row, key, default)


def _planning_metrics(
    rows: list[Any], planning_dates: list[Any] | tuple[Any, ...] = ()
) -> dict[str, Any]:
    """Build immutable per-day and per-engineer metrics for a plan version."""
    day_routes: dict[str, dict[int, dict[str, Any]]] = {
        str(_as_date(value)): {} for value in planning_dates
    }
    engineer_totals: dict[int, dict[str, Any]] = {}
    for row in rows:
        planning_date = str(_row_field(row, "planning_date"))
        engineer_id = int(_row_field(row, "engineer_id"))
        engineer_name = _row_field(row, "engineer_name")
        distance_meters = int(_row_field(row, "distance_from_previous_meters", 0) or 0)
        route = day_routes.setdefault(planning_date, {}).setdefault(
            engineer_id,
            {
                "engineer_id": engineer_id,
                "engineer_name": engineer_name,
                "assigned_jobs_count": 0,
                "distance_meters": 0,
            },
        )
        if not route.get("engineer_name") and engineer_name:
            route["engineer_name"] = engineer_name
        route["assigned_jobs_count"] += 1
        route["distance_meters"] += distance_meters

        total = engineer_totals.setdefault(
            engineer_id,
            {
                "engineer_id": engineer_id,
                "engineer_name": engineer_name,
                "active_days_count": 0,
                "assigned_jobs_count": 0,
                "distance_meters": 0,
            },
        )
        if not total.get("engineer_name") and engineer_name:
            total["engineer_name"] = engineer_name
        total["assigned_jobs_count"] += 1
        total["distance_meters"] += distance_meters

    days = []
    for planning_date, routes_by_engineer in sorted(day_routes.items()):
        routes = [
            routes_by_engineer[engineer_id]
            for engineer_id in sorted(routes_by_engineer)
        ]
        for route in routes:
            engineer_totals[int(route["engineer_id"])]["active_days_count"] += 1
        days.append(
            {
                "planning_date": planning_date,
                "personnel_count": len(routes),
                "assigned_jobs_count": sum(
                    int(route["assigned_jobs_count"]) for route in routes
                ),
                "distance_meters": sum(
                    int(route["distance_meters"]) for route in routes
                ),
                "engineers": routes,
            }
        )

    engineers_values = [
        engineer_totals[engineer_id] for engineer_id in sorted(engineer_totals)
    ]
    return {
        "days": days,
        "engineers": engineers_values,
        "totals": {
            "personnel_count": len(engineers_values),
            "assigned_jobs_count": sum(
                int(item["assigned_jobs_count"]) for item in engineers_values
            ),
            "distance_meters": sum(
                int(item["distance_meters"]) for item in engineers_values
            ),
        },
    }


def _classify_carried_forward_jobs(
    input_changes: dict[str, Any],
    current_assignments: list[Any],
    current_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Separate jobs outside the new calculation horizon from real removals."""
    result = {**input_changes, "carried_forward_job_ids": []}
    effective_start_value = (current_snapshot.get("project") or {}).get(
        "effective_start_date"
    )
    if not effective_start_value:
        return result
    effective_start = _as_date(effective_start_value)
    removed_ids = {int(value) for value in input_changes.get("removed_job_ids", [])}
    carried_ids = sorted(
        {
            int(_row_field(row, "job_id"))
            for row in current_assignments
            if int(_row_field(row, "job_id")) in removed_ids
            and _as_date(_row_field(row, "planning_date")) < effective_start
        }
    )
    result["carried_forward_job_ids"] = carried_ids
    result["removed_job_ids"] = sorted(removed_ids - set(carried_ids))
    return result


def _planning_input_changes(
    previous: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    """Return concrete input differences without claiming solver causality."""
    empty = {
        "snapshot_available": False,
        "new_job_ids": [],
        "removed_job_ids": [],
        "carried_forward_job_ids": [],
        "changed_job_ids": [],
        "job_changes": [],
        "schedule_changes": [],
        "engineer_changes": [],
        "equipment_changes": [],
        "required_equipment_changed_work_type_ids": [],
        "required_qualification_changed_work_type_ids": [],
        "engineer_qualification_changed_ids": [],
        "config_changed_fields": [],
        "horizon_changed": False,
        "horizon_change": {},
    }
    if not previous or not current:
        return empty

    def indexed(values: list[dict[str, Any]], key) -> dict[Any, dict[str, Any]]:
        return {key(item): item for item in values}

    def comparable(value: dict[str, Any]) -> dict[str, Any]:
        return {
            key: item
            for key, item in value.items()
            if key not in {"created_at", "updated_at"}
        }

    def changed_fields(
        before: dict[str, Any] | None, after: dict[str, Any] | None
    ) -> list[str]:
        left = comparable(before or {})
        right = comparable(after or {})
        return sorted(
            field
            for field in set(left) | set(right)
            if left.get(field) != right.get(field)
        )

    def field_changes(
        before: dict[str, Any] | None, after: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        left = comparable(before or {})
        right = comparable(after or {})
        return [
            {
                "field": field,
                "before": _jsonable(left.get(field)),
                "after": _jsonable(right.get(field)),
            }
            for field in changed_fields(before, after)
        ]

    def changed_mapping_ids(key: str, identifiers: set[int]) -> list[int]:
        left = {
            str(identifier): value
            for identifier, value in previous.get(key, {}).items()
        }
        right = {
            str(identifier): value for identifier, value in current.get(key, {}).items()
        }
        return sorted(
            int(identifier)
            for identifier in identifiers
            if left.get(str(identifier), []) != right.get(str(identifier), [])
        )

    previous_jobs = indexed(previous.get("jobs", []), lambda item: int(item["id"]))
    current_jobs = indexed(current.get("jobs", []), lambda item: int(item["id"]))
    previous_engineers = indexed(
        previous.get("engineers", []), lambda item: int(item["id"])
    )
    current_engineers = indexed(
        current.get("engineers", []), lambda item: int(item["id"])
    )
    previous_project = previous.get("project", {})
    current_project = current.get("project", {})
    previous_start = str(previous_project.get("effective_start_date") or "")
    current_start = str(current_project.get("effective_start_date") or "")
    previous_end = str(previous_project.get("maximum_horizon_end") or "")
    current_end = str(current_project.get("maximum_horizon_end") or "")
    overlap_start = max(previous_start, current_start)
    overlap_end = min(previous_end, current_end)
    schedule_key = lambda item: (  # noqa: E731 - compact snapshot key definition
        int(item["engineer_id"]),
        str(item["work_date"]),
    )
    previous_schedules = indexed(previous.get("schedules", []), schedule_key)
    current_schedules = indexed(current.get("schedules", []), schedule_key)

    schedule_changes = []
    for engineer_id, planning_date in sorted(
        set(previous_schedules) | set(current_schedules), key=lambda value: value
    ):
        if (
            overlap_start
            and overlap_end
            and not (overlap_start <= planning_date <= overlap_end)
        ):
            continue
        before = previous_schedules.get((engineer_id, planning_date))
        after = current_schedules.get((engineer_id, planning_date))
        if before == after:
            continue
        schedule_changes.append(
            {
                "engineer_id": engineer_id,
                "engineer_name": (
                    (current_engineers.get(engineer_id) or {}).get("name")
                    or (previous_engineers.get(engineer_id) or {}).get("name")
                ),
                "planning_date": planning_date,
                "before": before,
                "after": after,
            }
        )

    engineer_changes = []
    for engineer_id in sorted(set(previous_engineers) | set(current_engineers)):
        before = previous_engineers.get(engineer_id)
        after = current_engineers.get(engineer_id)
        if comparable(before or {}) == comparable(after or {}):
            continue
        engineer_changes.append(
            {
                "engineer_id": engineer_id,
                "engineer_name": (after or before or {}).get("name"),
                "removed": after is None,
                "added": before is None,
                "changed_fields": changed_fields(before, after),
                "changes": field_changes(before, after),
            }
        )

    previous_units = {
        int(key): int(value or 0)
        for key, value in previous.get("equipment_units", {}).items()
    }
    current_units = {
        int(key): int(value or 0)
        for key, value in current.get("equipment_units", {}).items()
    }
    equipment_names = {
        int(item["id"]): item.get("name")
        for item in [
            *previous.get("equipment_types", []),
            *current.get("equipment_types", []),
        ]
    }
    equipment_changes = [
        {
            "equipment_type_id": equipment_id,
            "equipment_name": equipment_names.get(equipment_id),
            "before_units": previous_units.get(equipment_id, 0),
            "after_units": current_units.get(equipment_id, 0),
        }
        for equipment_id in sorted(set(previous_units) | set(current_units))
        if previous_units.get(equipment_id, 0) != current_units.get(equipment_id, 0)
    ]

    previous_config = previous.get("config", {})
    current_config = current.get("config", {})
    config_changed_fields = sorted(
        key
        for key in set(previous_config) | set(current_config)
        if previous_config.get(key) != current_config.get(key)
    )
    horizon_fields = ("effective_start_date", "maximum_horizon_end")
    horizon_change = {
        field: {
            "before": _jsonable(previous_project.get(field)),
            "after": _jsonable(current_project.get(field)),
        }
        for field in horizon_fields
        if previous_project.get(field) != current_project.get(field)
    }
    changed_job_ids = sorted(
        job_id
        for job_id in set(previous_jobs) & set(current_jobs)
        if comparable(previous_jobs[job_id]) != comparable(current_jobs[job_id])
    )
    previous_work_type_ids = {
        int(item["work_type_id"])
        for item in previous_jobs.values()
        if item.get("work_type_id") is not None
    }
    current_work_type_ids = {
        int(item["work_type_id"])
        for item in current_jobs.values()
        if item.get("work_type_id") is not None
    }
    common_work_type_ids = previous_work_type_ids & current_work_type_ids
    common_engineer_ids = set(previous_engineers) & set(current_engineers)

    return {
        "snapshot_available": True,
        "new_job_ids": sorted(set(current_jobs) - set(previous_jobs)),
        "removed_job_ids": sorted(set(previous_jobs) - set(current_jobs)),
        "carried_forward_job_ids": [],
        "changed_job_ids": changed_job_ids,
        "job_changes": [
            {
                "job_id": job_id,
                "changed_fields": changed_fields(
                    previous_jobs[job_id], current_jobs[job_id]
                ),
                "changes": field_changes(previous_jobs[job_id], current_jobs[job_id]),
            }
            for job_id in changed_job_ids
        ],
        "schedule_changes": schedule_changes,
        "engineer_changes": engineer_changes,
        "equipment_changes": equipment_changes,
        "required_equipment_changed_work_type_ids": changed_mapping_ids(
            "required_equipment", common_work_type_ids
        ),
        "required_qualification_changed_work_type_ids": changed_mapping_ids(
            "required_qualifications", common_work_type_ids
        ),
        "engineer_qualification_changed_ids": changed_mapping_ids(
            "engineer_qualifications", common_engineer_ids
        ),
        "config_changed_fields": config_changed_fields,
        "horizon_changed": bool(horizon_change),
        "horizon_change": horizon_change,
    }


def _route_metrics(rows: list[Any]) -> dict[str, Any]:
    routes: dict[tuple[str, int], dict[str, Any]] = {}
    active_engineer_ids: set[int] = set()
    for row in rows:
        planning_date = str(row.planning_date)
        engineer_id = int(row.engineer_id)
        if row.status in {"NEW", "IN_PROGRESS"}:
            active_engineer_ids.add(engineer_id)
        key = (planning_date, engineer_id)
        route = routes.setdefault(
            key,
            {
                "planning_date": planning_date,
                "engineer_id": engineer_id,
                "engineer_name": row.engineer_name,
                "distance_meters": 0,
            },
        )
        route["distance_meters"] += int(row.distance_from_previous_meters or 0)
    items = sorted(
        routes.values(), key=lambda item: (item["planning_date"], item["engineer_id"])
    )
    distances = [int(item["distance_meters"]) for item in items]
    return {
        "active_engineers": len(active_engineer_ids),
        "total_distance_meters": sum(distances),
        "maximum_route_distance_meters": max(distances, default=0),
        "routes": items,
    }


def _planning_phase(state: str, current_day: Any | None) -> str:
    if state == "PENDING":
        return "Подготовка данных"
    if state == "FAILED":
        return "Ошибка"
    if current_day is not None:
        if current_day.status == "RUNNING":
            return f"Расчёт {current_day.planning_date:%d.%m}"
        if current_day.status == "PENDING":
            return "Подготовка данных"
        return "Проверка результата"
    return "Публикация"


def _published_plan_status(
    batch_status: str | None, _feasible_time_limit: bool = False
) -> str:
    """Expose publication outcome separately from solver search quality."""
    return "PARTIAL" if batch_status == "PARTIAL" else "SUCCESS"


def _safe_planning_error(event: Any) -> str | None:
    if event.state != "FAILED":
        return None
    labels = {
        "STALE_SNAPSHOT": "Исходные данные изменились во время расчёта",
        "TRAVEL_PROVIDER_UNAVAILABLE": "Дорожный сервис временно недоступен",
        "DISTANCE_DATA_NOT_READY": "Дорожные данные ещё не готовы",
        "FAILED_VALIDATION": "Результат не прошёл проверку",
        "CANDIDATE_COMPARISON_TIMEOUT": "Расчёт превысил допустимое время",
        "OBJECTIVE_RANGE_OVERFLOW": (
            "Превышен числовой предел оценки маршрутов. "
            "Необходимо изменить настройки или способ расчёта"
        ),
        "SYSTEM_ERROR": "Не удалось завершить расчёт",
    }
    return labels.get(str(event.error_code), "Не удалось завершить расчёт")


def _coordinate(
    value: dict[str, Any],
    *,
    latitude_key: str = "latitude",
    longitude_key: str = "longitude",
) -> dict[str, Any] | None:
    # Normalized solver snapshots store coordinates as an embedded value;
    # source snapshots and database projections use flat columns.
    if latitude_key == "latitude" and longitude_key == "longitude":
        nested = value.get("coordinate")
        if isinstance(nested, dict):
            value = {**value, **nested}
    latitude = value.get(latitude_key)
    longitude = value.get(longitude_key)
    if latitude is None or longitude is None:
        return None
    return _jsonable({"latitude": latitude, "longitude": longitude})


def _eligible_engineer_count(
    job: dict[str, Any], engineers_snapshot: list[dict[str, Any]]
) -> int:
    required = {int(value) for value in job.get("required_qualifications", [])}
    required_transport = job.get("required_transport")
    allowed = job.get("allowed_engineer_ids")
    allowed_ids = {int(value) for value in allowed} if allowed is not None else None
    count = 0
    for engineer in engineers_snapshot:
        engineer_id = int(engineer.get("id", engineer.get("engineer_id", 0)))
        if allowed_ids is not None and engineer_id not in allowed_ids:
            continue
        qualifications = {int(value) for value in engineer.get("qualifications", [])}
        if not required.issubset(qualifications):
            continue
        transport = str(engineer.get("transport_type") or "NONE")
        if required_transport == "CAR" and transport != "CAR":
            continue
        count += 1
    return count


def _is_overdue(value: Any, planning_date: date) -> bool:
    if value is None:
        return False
    candidate = (
        value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    )
    return candidate < planning_date
