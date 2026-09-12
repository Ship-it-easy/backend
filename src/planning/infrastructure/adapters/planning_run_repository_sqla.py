import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.errors import (
    InvalidPlanningRequest,
    PlanningRunInProgress,
    PlanningRunNotFound,
    PlanningUnavailable,
    ProjectNotFound,
)
from planning.domain.enums import PlanningRunStatus
from planning.domain.entities.planning import PlanningInput, PlanningResult
from planning.infrastructure.persistence_sqla.mappings.tables import (
    engineer_qualifications,
    engineer_schedules,
    engineers,
    equipment_types,
    jobs,
    planning_config,
    planning_equipment_assignments,
    planning_route_jobs,
    planning_routes,
    planning_runs,
    planning_unassigned_jobs,
    projects,
    work_type_required_equipment,
    work_type_required_qualifications,
    work_types,
)


class SqlaPlanningRunRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_project_timezone(self, project_id: int) -> str:
        row = (
            await self._session.execute(
                select(projects.c.planning_timezone, projects.c.status).where(
                    projects.c.id == project_id
                )
            )
        ).one_or_none()
        if row is None:
            raise ProjectNotFound("Project not found")
        if row.status != "ACTIVE":
            raise PlanningUnavailable("Project is blocked")
        return str(row.planning_timezone)

    async def create_run(
        self,
        project_id: int,
        planning_date: date,
        timezone_name: str,
        initiated_by_user_id: Any | None = None,
    ) -> int:
        project = (
            (
                await self._session.execute(
                    select(projects).where(projects.c.id == project_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if project is None:
            raise ProjectNotFound("Project not found")
        if project.planning_timezone != timezone_name:
            raise InvalidPlanningRequest(
                "timezone must match the project planning timezone"
            )
        if not project.planning_one_day_enabled:
            raise PlanningUnavailable("One-day planning is not enabled for project")
        if project.status != "ACTIVE":
            raise PlanningUnavailable("Project is blocked")
        try:
            run_id = await self._session.scalar(
                insert(planning_runs)
                .values(
                    project_id=project_id,
                    planning_date=planning_date,
                    timezone=timezone_name,
                    status=PlanningRunStatus.PREPARING.value,
                    started_at=datetime.now(timezone.utc),
                    initiated_by_user_id=initiated_by_user_id,
                )
                .returning(planning_runs.c.id)
            )
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise PlanningRunInProgress(
                "A planning run is already active for this project and date"
            ) from error
        return int(run_id)

    async def load_source(self, project_id: int, planning_date: date) -> dict[str, Any]:
        await self._session.connection(
            execution_options={"isolation_level": "REPEATABLE READ"}
        )
        project = (
            (
                await self._session.execute(
                    select(projects).where(projects.c.id == project_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if project is None:
            raise ProjectNotFound("Project not found")
        config = (
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

        job_rows = (
            (
                await self._session.execute(
                    select(jobs, work_types)
                    .join(work_types, jobs.c.work_type_id == work_types.c.id)
                    .where(
                        jobs.c.project_id == project_id,
                        work_types.c.project_id == project_id,
                        jobs.c.status == "NEW",
                    )
                    .order_by(jobs.c.id)
                )
            )
            .mappings()
            .all()
        )
        engineer_rows = (
            (
                await self._session.execute(
                    select(engineers, engineer_schedules)
                    .join(
                        engineer_schedules,
                        and_(
                            engineer_schedules.c.engineer_id == engineers.c.id,
                            engineer_schedules.c.work_date == planning_date,
                        ),
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
        work_type_ids = {row.work_type_id for row in job_rows}
        engineer_ids = {row.engineer_id for row in engineer_rows}
        required_qualifications = await self._pairs_by_left(
            work_type_required_qualifications,
            "work_type_id",
            "qualification_id",
            work_type_ids,
        )
        required_equipment = await self._pairs_by_left(
            work_type_required_equipment,
            "work_type_id",
            "equipment_type_id",
            work_type_ids,
        )
        engineer_quals = await self._pairs_by_left(
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
                        equipment_types.c.available_units,
                    ).where(
                        equipment_types.c.project_id == project_id,
                        equipment_types.c.active.is_(True),
                    )
                )
            )
            .mappings()
            .all()
        )
        result = {
            "project": dict(project),
            "config": dict(config) if config else None,
            "jobs": [dict(row) for row in job_rows],
            "engineers": [dict(row) for row in engineer_rows],
            "required_qualifications": required_qualifications,
            "required_equipment": required_equipment,
            "engineer_qualifications": engineer_quals,
            "equipment_units": {
                int(row.id): int(row.available_units)
                for row in equipment_rows
            },
        }
        await self._session.commit()
        return result

    async def _pairs_by_left(
        self,
        table,
        left_name: str,
        right_name: str,
        ids: set[int],
    ) -> dict[int, set[int]]:
        result: dict[int, set[int]] = {int(item): set() for item in ids}
        if not ids:
            return result
        left = table.c[left_name]
        rows = (
            await self._session.execute(select(table).where(left.in_(ids)))
        ).mappings()
        for row in rows:
            result[int(row[left_name])].add(int(row[right_name]))
        return result

    async def mark_running(self, run_id: int, data: PlanningInput) -> None:
        snapshot = _jsonable(data.snapshot)
        normalized_hash = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        await self._session.execute(
            update(planning_runs)
            .where(planning_runs.c.id == run_id)
            .values(
                status=PlanningRunStatus.RUNNING.value,
                config_snapshot=_jsonable(asdict(data.config)),
                input_snapshot=snapshot,
                normalized_input_hash=normalized_hash,
                input_jobs_count=data.input_jobs_count,
                eligible_jobs_count=len(data.jobs),
                traffic_reference_time=_traffic_reference_time(data),
            )
        )
        await self._session.commit()

    async def save_result(
        self, run_id: int, data: PlanningInput, result: PlanningResult
    ) -> None:
        status = (
            PlanningRunStatus.FAILED_VALIDATION.value
            if result.validation_errors
            else PlanningRunStatus.SUCCESS.value
        )
        assigned_ids = {job.job_id for route in result.routes for job in route.jobs}
        sla_critical = set(data.sla_critical_job_ids)
        matrix_hash = (
            hashlib.sha256(
                json.dumps(
                    result.travel_matrices,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            if result.travel_matrices
            else None
        )
        for route in result.routes:
            route_id = await self._session.scalar(
                insert(planning_routes)
                .values(
                    planning_run_id=run_id,
                    engineer_id=route.engineer_id,
                    planned_start=route.planned_start,
                    planned_finish=route.planned_finish,
                    total_travel_min=route.total_travel_min,
                    total_service_min=route.total_service_min,
                    total_waiting_min=route.total_waiting_min,
                )
                .returning(planning_routes.c.id)
            )
            if route.jobs:
                await self._session.execute(
                    insert(planning_route_jobs),
                    [
                        {
                            "planning_route_id": route_id,
                            "planning_run_id": run_id,
                            "job_id": item.job_id,
                            "sequence": item.sequence,
                            "planned_arrival": item.planned_arrival,
                            "planned_start": item.planned_start,
                            "planned_finish": item.planned_finish,
                            "travel_from_previous_min": item.travel_from_previous_min,
                            "waiting_before_job_min": item.waiting_before_job_min,
                            "drop_penalty_snapshot": item.drop_penalty,
                        }
                        for item in route.jobs
                    ],
                )
            if route.equipment_type_ids:
                await self._session.execute(
                    insert(planning_equipment_assignments),
                    [
                        {
                            "planning_run_id": run_id,
                            "engineer_id": route.engineer_id,
                            "equipment_type_id": equipment_id,
                        }
                        for equipment_id in route.equipment_type_ids
                    ],
                )
        if result.unassigned:
            await self._session.execute(
                insert(planning_unassigned_jobs),
                [
                    {
                        "planning_run_id": run_id,
                        "job_id": item.job_id,
                        "drop_penalty": item.drop_penalty,
                        "primary_reason_code": item.reason_code.value,
                        "diagnostic_flags": item.diagnostic_flags,
                    }
                    for item in result.unassigned
                ],
            )
        await self._session.execute(
            update(planning_runs)
            .where(planning_runs.c.id == run_id)
            .values(
                status=status,
                solver_status=result.solver_status,
                finished_at=datetime.now(timezone.utc),
                objective=result.objective,
                drop_cost=result.drop_cost,
                travel_cost=result.travel_cost,
                assigned_jobs_count=len(assigned_ids),
                unassigned_jobs_count=len(result.unassigned),
                sla_critical_total=len(sla_critical),
                sla_critical_assigned=len(sla_critical & assigned_ids),
                solver_version=_ortools_version(),
                solver_time_ms=result.solver_time_ms,
                travel_matrix_hash=matrix_hash,
                validation_errors=result.validation_errors,
            )
        )
        await self._session.commit()

    async def fail_run(self, run_id: int, code: str, message: str) -> None:
        await self._session.rollback()
        await self._session.execute(
            update(planning_runs)
            .where(planning_runs.c.id == run_id)
            .values(
                status=PlanningRunStatus.FAILED.value,
                finished_at=datetime.now(timezone.utc),
                error_code=code,
                error_message=message[:1000],
            )
        )
        await self._session.commit()

    async def get_run(self, project_id: int, run_id: int) -> dict[str, Any]:
        run = (
            (
                await self._session.execute(
                    select(planning_runs).where(
                        planning_runs.c.id == run_id,
                        planning_runs.c.project_id == project_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if run is None:
            raise PlanningRunNotFound("Planning run not found")
        route_rows = (
            (
                await self._session.execute(
                    select(planning_routes)
                    .where(planning_routes.c.planning_run_id == run_id)
                    .order_by(planning_routes.c.id)
                )
            )
            .mappings()
            .all()
        )
        job_rows = (
            (
                await self._session.execute(
                    select(planning_route_jobs)
                    .where(planning_route_jobs.c.planning_run_id == run_id)
                    .order_by(
                        planning_route_jobs.c.planning_route_id,
                        planning_route_jobs.c.sequence,
                    )
                )
            )
            .mappings()
            .all()
        )
        equipment_rows = (
            (
                await self._session.execute(
                    select(planning_equipment_assignments).where(
                        planning_equipment_assignments.c.planning_run_id == run_id
                    )
                )
            )
            .mappings()
            .all()
        )
        unassigned = (
            (
                await self._session.execute(
                    select(planning_unassigned_jobs).where(
                        planning_unassigned_jobs.c.planning_run_id == run_id
                    )
                )
            )
            .mappings()
            .all()
        )
        jobs_by_route: dict[int, list[dict[str, Any]]] = {}
        for row in job_rows:
            jobs_by_route.setdefault(int(row.planning_route_id), []).append(dict(row))
        equipment_by_engineer: dict[int, list[int]] = {}
        for row in equipment_rows:
            equipment_by_engineer.setdefault(int(row.engineer_id), []).append(
                int(row.equipment_type_id)
            )
        routes_result = []
        for row in route_rows:
            value = dict(row)
            value["jobs"] = jobs_by_route.get(int(row.id), [])
            value["equipment_type_ids"] = equipment_by_engineer.get(
                int(row.engineer_id), []
            )
            routes_result.append(value)
        return _jsonable(
            {
                "run": dict(run),
                "routes": routes_result,
                "unassigned_jobs": [dict(row) for row in unassigned],
            }
        )

    async def list_runs(
        self,
        project_id: int,
        planning_date: date | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        query = select(planning_runs).where(planning_runs.c.project_id == project_id)
        if planning_date:
            query = query.where(planning_runs.c.planning_date == planning_date)
        if status:
            query = query.where(planning_runs.c.status == status)
        rows = (
            (
                await self._session.execute(
                    query.order_by(planning_runs.c.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .mappings()
            .all()
        )
        return _jsonable([dict(row) for row in rows])


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "as_tuple"):
        return str(value)
    return value


def _ortools_version() -> str:
    try:
        import ortools

        return ortools.__version__
    except Exception:
        return "unknown"


def _traffic_reference_time(data: PlanningInput) -> datetime:
    zone = ZoneInfo(data.timezone)
    now_local = datetime.now(timezone.utc).astimezone(zone)
    earliest_shift_min = min(
        (engineer.shift_start_min for engineer in data.engineers),
        default=0,
    )
    shift_reference = datetime(
        data.planning_date.year,
        data.planning_date.month,
        data.planning_date.day,
        tzinfo=zone,
    ) + timedelta(minutes=earliest_shift_min)
    if data.planning_date == now_local.date():
        return max(now_local, shift_reference).astimezone(timezone.utc)
    return shift_reference.astimezone(timezone.utc)
