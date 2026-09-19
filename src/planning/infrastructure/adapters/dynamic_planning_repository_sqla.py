import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.validators.dynamic_plan import DynamicPlanValidator
from planning.infrastructure.adapters.planning_batch_repository_sqla import (
    SqlaPlanningBatchRepository,
    _jsonable,
)
from planning.infrastructure.persistence_sqla.mappings.tables import (
    candidate_evaluations,
    engineers,
    job_planning_state,
    job_status_history,
    jobs,
    plan_changes,
    planning_batch_days,
    planning_batch_jobs,
    planning_batches,
    planning_events,
    planning_route_jobs,
    planning_routes,
    planning_runs,
    project_plan_assignments,
    project_plan_versions,
    projects,
    work_types,
)


class StaleDynamicSnapshot(RuntimeError):
    pass


class SqlaDynamicPlanningRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def enqueue(
        self,
        project_id: int,
        event_type: str,
        actor_user_id: Any,
        idempotency_key: str,
        job_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

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
        return _jsonable(dict(row))

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
                            jobs.c.address,
                            jobs.c.latitude,
                            jobs.c.longitude,
                            jobs.c.sla_date,
                            jobs.c.time_window_start,
                            jobs.c.time_window_end,
                            jobs.c.work_type_id,
                            jobs.c.service_duration_min,
                            jobs.c.created_at.label("job_created_at"),
                            jobs.c.priority_type,
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
        await self._session.execute(
            update(planning_events)
            .where(planning_events.c.id.in_(event_ids))
            .values(
                state="PUBLISHED",
                published_plan_version_id=version_id,
                input_hash=input_hash,
                finished_at=datetime.now(timezone.utc),
            )
        )
        await self._session.commit()

    async def fail(
        self, event_ids: list[int], code: str, message: str, *, retry: bool = False
    ) -> None:
        await self._session.rollback()
        await self._session.execute(
            update(planning_events)
            .where(planning_events.c.id.in_(event_ids))
            .values(
                state="PENDING" if retry else "FAILED",
                error_code=code,
                error_message=message[:1000],
                finished_at=None if retry else datetime.now(timezone.utc),
            )
        )
        await self._session.commit()

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
            raise RuntimeError(
                "FAILED_VALIDATION: " + "; ".join(validation_errors)
            )
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
            "maximum_route_distance_meters": max(
                route_distances.values(), default=0
            ),
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
        rows = (
            (
                await self._session.execute(
                    select(
                        project_plan_assignments,
                        jobs.c.address,
                        jobs.c.status,
                        jobs.c.priority_type,
                        jobs.c.latitude,
                        jobs.c.longitude,
                        engineers.c.name.label("engineer_name"),
                        engineers.c.transport_type,
                        engineers.c.start_address.label("engineer_start_address"),
                        engineers.c.start_latitude,
                        engineers.c.start_longitude,
                    )
                    .join(jobs, jobs.c.id == project_plan_assignments.c.job_id)
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
        serialized_changes = []
        for row in changes:
            item = _jsonable(dict(row))
            if item["change_type"] == "CHANGED" and _same_assignment(
                item.get("old_assignment"), item.get("new_assignment")
            ):
                # Versions published before assignment values were normalized may
                # contain a false diff (JSON date string versus Python date).
                continue
            serialized_changes.append(item)
        return {
            "version": _jsonable(dict(version)),
            "timezone": str(timezone_name),
            "assignments": [_jsonable(dict(row)) for row in rows],
            "changes": serialized_changes,
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


def _changes(
    old: dict[int, dict[str, Any]],
    new: dict[int, dict[str, Any]],
    version_id: int,
    project_id: int,
) -> list[dict[str, Any]]:
    result = []
    for job_id in sorted(set(old) | set(new)):
        before = _jsonable(old.get(job_id))
        after = _jsonable(new.get(job_id))
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


def _same_assignment(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> bool:
    if before is None or after is None:
        return before is after
    keys = (
        "planning_date",
        "engineer_id",
        "sequence",
        "planned_start",
        "planned_finish",
        "requirement_snapshot",
    )
    normalized_before = _jsonable(before)
    normalized_after = _jsonable(after)
    return all(normalized_before.get(key) == normalized_after.get(key) for key in keys)


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
