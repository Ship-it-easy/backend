from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.errors import ConflictError, ObjectNotFoundError
from planning.application.interfaces.project_management_repositories import (
    PlanningManagementRepository,
)
from planning.application.management_dto import (
    AssignmentSnapshot,
    PlanningPublicationState,
    PlanningReadinessState,
    PlanningRouteAssignment,
    PublishedPlan,
    PublishPlanCommand,
)
from planning.infrastructure.persistence_sqla.mappings.tables import (
    assignments,
    daily_plans,
    engineer_schedules,
    engineers,
    jobs,
    plan_versions,
    planning_config,
    planning_route_jobs,
    planning_routes,
    planning_runs,
    work_types,
)


class SqlaPlanningManagementRepository(PlanningManagementRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_config(self, project_id: int) -> dict[str, Any]:
        return dict(await self._active_config(project_id))

    async def update_config(
        self, project_id: int, values: dict[str, Any]
    ) -> dict[str, Any]:
        current = await self._active_config(project_id, for_update=True)
        next_values = {
            key: value
            for key, value in dict(current).items()
            if key not in {"id", "created_at", "updated_at", "active"}
        }
        next_values.update(values)
        next_values.update(version=current.version + 1, active=True)
        await self._session.execute(
            update(planning_config)
            .where(planning_config.c.id == current.id)
            .values(active=False, updated_at=datetime.now(timezone.utc))
        )
        row = (
            (
                await self._session.execute(
                    insert(planning_config)
                    .values(**next_values)
                    .returning(*planning_config.c)
                )
            )
            .mappings()
            .one()
        )
        await self._session.commit()
        return dict(row)

    async def get_readiness_state(
        self, project_id: int, planning_date: date
    ) -> PlanningReadinessState:
        checks = {
            "PLANNING_CONFIG_MISSING": await self._session.scalar(
                select(planning_config.c.id).where(
                    planning_config.c.project_id == project_id,
                    planning_config.c.active.is_(True),
                )
            ),
            "WORK_TYPES_MISSING": await self._session.scalar(
                select(work_types.c.id)
                .where(
                    work_types.c.project_id == project_id, work_types.c.active.is_(True)
                )
                .limit(1)
            ),
            "ENGINEERS_MISSING": await self._session.scalar(
                select(engineers.c.id)
                .join(
                    engineer_schedules,
                    engineer_schedules.c.engineer_id == engineers.c.id,
                )
                .where(
                    engineers.c.project_id == project_id,
                    engineers.c.active.is_(True),
                    engineer_schedules.c.work_date == planning_date,
                )
                .limit(1)
            ),
            "JOBS_MISSING": await self._session.scalar(
                select(jobs.c.id)
                .where(jobs.c.project_id == project_id, jobs.c.status == "NEW")
                .limit(1)
            ),
        }
        return PlanningReadinessState(
            has_config=checks["PLANNING_CONFIG_MISSING"] is not None,
            has_work_types=checks["WORK_TYPES_MISSING"] is not None,
            has_engineers=checks["ENGINEERS_MISSING"] is not None,
            has_jobs=checks["JOBS_MISSING"] is not None,
        )

    async def load_publication_state(
        self, project_id: int, run_id: int
    ) -> PlanningPublicationState | None:
        run = (
            (
                await self._session.execute(
                    select(planning_runs)
                    .where(
                        planning_runs.c.id == run_id,
                        planning_runs.c.project_id == project_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if run is None:
            return None
        already_published = (
            await self._session.scalar(
                select(plan_versions.c.id).where(
                    plan_versions.c.planning_run_id == run_id
                )
            )
            is not None
        )
        plan = (
            (
                await self._session.execute(
                    select(daily_plans)
                    .where(
                        daily_plans.c.project_id == project_id,
                        daily_plans.c.planning_date == run.planning_date,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        plan_id = int(plan.id) if plan is not None else None
        current_version_id = (
            int(plan.current_version_id)
            if plan is not None and plan.current_version_id is not None
            else None
        )
        max_version = 0
        if plan_id is not None:
            max_version = int(
                await self._session.scalar(
                    select(
                        func.coalesce(func.max(plan_versions.c.version_number), 0)
                    ).where(plan_versions.c.daily_plan_id == plan_id)
                )
                or 0
            )
        current_plan_started = False
        if current_version_id is not None:
            current_plan_started = (
                await self._session.scalar(
                    select(jobs.c.id)
                    .join(assignments, assignments.c.job_id == jobs.c.id)
                    .where(
                        assignments.c.plan_version_id == current_version_id,
                        jobs.c.status.in_(["IN_PROGRESS", "COMPLETED"]),
                    )
                    .with_for_update()
                    .limit(1)
                )
                is not None
            )
        route_rows = (
            (
                await self._session.execute(
                    select(planning_route_jobs, planning_routes.c.engineer_id)
                    .join(
                        planning_routes,
                        planning_route_jobs.c.planning_route_id == planning_routes.c.id,
                    )
                    .where(planning_route_jobs.c.planning_run_id == run_id)
                    .order_by(
                        planning_routes.c.engineer_id, planning_route_jobs.c.sequence
                    )
                )
            )
            .mappings()
            .all()
        )
        routes = tuple(
            PlanningRouteAssignment(
                job_id=int(item.job_id),
                engineer_id=int(item.engineer_id),
                sequence=int(item.sequence),
                planned_arrival=item.planned_arrival,
                planned_start=item.planned_start,
                planned_finish=item.planned_finish,
                travel_from_previous_min=int(item.travel_from_previous_min),
                waiting_before_job_min=int(item.waiting_before_job_min),
            )
            for item in route_rows
        )
        assigned_ids = [item.job_id for item in routes]
        publishable_ids: frozenset[int] = frozenset()
        if assigned_ids:
            publishable_ids = frozenset(
                int(value)
                for value in (
                    await self._session.scalars(
                        select(jobs.c.id)
                        .where(
                            jobs.c.id.in_(assigned_ids),
                            jobs.c.project_id == project_id,
                            jobs.c.status == "NEW",
                        )
                        .order_by(jobs.c.id)
                        .with_for_update()
                    )
                ).all()
            )
        raw_snapshots = {
            int(item["id"]): item for item in run.input_snapshot.get("jobs", [])
        }
        snapshots = {
            job_id: AssignmentSnapshot(
                required_qualifications=tuple(value.get("required_qualifications", [])),
                required_equipment=tuple(value.get("required_equipment", [])),
                required_transport=value.get("required_transport"),
            )
            for job_id, value in raw_snapshots.items()
        }
        return PlanningPublicationState(
            run_id=int(run.id),
            planning_date=run.planning_date,
            status=run.status,
            validation_errors=tuple(run.validation_errors or ()),
            unassigned_jobs_count=int(run.unassigned_jobs_count or 0),
            already_published=already_published,
            daily_plan_id=plan_id,
            current_version_id=current_version_id,
            current_plan_started=current_plan_started,
            max_version_number=max_version,
            routes=routes,
            publishable_job_ids=publishable_ids,
            snapshots=snapshots,
        )

    async def save_publication(self, command: PublishPlanCommand) -> PublishedPlan:
        try:
            plan_id = command.daily_plan_id
            if plan_id is None:
                plan_id = await self._session.scalar(
                    insert(daily_plans)
                    .values(
                        project_id=command.project_id,
                        planning_date=command.planning_date,
                    )
                    .returning(daily_plans.c.id)
                )
            if command.previous_version_id is not None:
                await self._session.execute(
                    update(plan_versions)
                    .where(plan_versions.c.id == command.previous_version_id)
                    .values(
                        status="SUPERSEDED", superseded_at=datetime.now(timezone.utc)
                    )
                )
            version_id = await self._session.scalar(
                insert(plan_versions)
                .values(
                    daily_plan_id=plan_id,
                    version_number=command.version_number,
                    planning_run_id=command.run_id,
                    status="PUBLISHED",
                    published_by=command.published_by,
                )
                .returning(plan_versions.c.id)
            )
            for item in command.assignments:
                snapshot = command.snapshots.get(item.job_id, AssignmentSnapshot())
                await self._session.execute(
                    insert(assignments).values(
                        plan_version_id=version_id,
                        job_id=item.job_id,
                        engineer_id=item.engineer_id,
                        sequence=item.sequence,
                        planned_start=item.planned_start,
                        planned_finish=item.planned_finish,
                        route_data={
                            "planned_arrival": item.planned_arrival.isoformat(),
                            "travel_from_previous_min": item.travel_from_previous_min,
                            "waiting_before_job_min": item.waiting_before_job_min,
                        },
                        requirement_snapshot={
                            "required_qualifications": list(
                                snapshot.required_qualifications
                            ),
                            "required_equipment": list(snapshot.required_equipment),
                            "required_transport": snapshot.required_transport,
                        },
                    )
                )
            await self._session.execute(
                update(daily_plans)
                .where(daily_plans.c.id == plan_id)
                .values(
                    current_version_id=version_id, updated_at=datetime.now(timezone.utc)
                )
            )
        except IntegrityError as error:
            raise ConflictError(
                "Planning run was published concurrently",
                code="CONCURRENT_MODIFICATION",
            ) from error
        return PublishedPlan(
            int(plan_id),
            int(version_id),
            command.version_number,
            len(command.assignments),
        )

    async def get_daily_plan(
        self, project_id: int, planning_date: date
    ) -> dict[str, Any]:
        plan = (
            (
                await self._session.execute(
                    select(daily_plans, plan_versions)
                    .join(
                        plan_versions,
                        daily_plans.c.current_version_id == plan_versions.c.id,
                    )
                    .where(
                        daily_plans.c.project_id == project_id,
                        daily_plans.c.planning_date == planning_date,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if plan is None:
            raise ObjectNotFoundError("Published daily plan not found")
        rows = (
            (
                await self._session.execute(
                    select(
                        assignments, jobs.c.status, jobs.c.address, jobs.c.work_type_id
                    )
                    .join(jobs, assignments.c.job_id == jobs.c.id)
                    .where(
                        assignments.c.plan_version_id == plan.current_version_id,
                        assignments.c.active.is_(True),
                    )
                    .order_by(assignments.c.engineer_id, assignments.c.sequence)
                )
            )
            .mappings()
            .all()
        )
        statuses = [row.status for row in rows]
        return {
            "id": plan.id,
            "project_id": project_id,
            "planning_date": planning_date,
            "version": plan.version_number,
            "planning_run_id": plan.planning_run_id,
            "status": _plan_state(statuses, plan.status),
            "assignments": [dict(row) for row in rows],
        }

    async def _active_config(self, project_id: int, *, for_update: bool = False) -> Any:
        query = select(planning_config).where(
            planning_config.c.project_id == project_id,
            planning_config.c.active.is_(True),
        )
        if for_update:
            query = query.with_for_update()
        row = (await self._session.execute(query)).mappings().one_or_none()
        if row is None:
            raise ConflictError(
                "Project has no active planning configuration",
                code="PLANNING_CONFIG_MISSING",
            )
        return row


def _plan_state(statuses: list[str], version_status: str) -> str:
    if version_status == "SUPERSEDED":
        return "SUPERSEDED"
    if not statuses:
        return "PUBLISHED"
    if all(value in {"COMPLETED", "CANCELLED"} for value in statuses):
        return "COMPLETED"
    if any(value in {"IN_PROGRESS", "COMPLETED"} for value in statuses):
        return "IN_PROGRESS"
    return "PUBLISHED"
