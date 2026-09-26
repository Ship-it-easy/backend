import hashlib
import json
import logging
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import UUID
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
from planning.domain.entities.planning import PlanningInput, PlanningResult
from planning.domain.enums import ACTIVE_BATCH_STATUSES, PlanningRunStatus
from planning.infrastructure.adapters.baseline_audit_persistence import (
    replace_baseline_audit_rows,
)
from planning.infrastructure.persistence_sqla.mappings.tables import (
    engineer_qualifications,
    engineer_schedules,
    engineers,
    equipment_types,
    jobs,
    planning_baseline_results,
    planning_batches,
    planning_config,
    planning_equipment_assignments,
    planning_plan_comparisons,
    planning_route_jobs,
    planning_routes,
    planning_runs,
    planning_unassigned_jobs,
    projects,
    work_type_required_equipment,
    work_type_required_qualifications,
    work_types,
)

logger = logging.getLogger(__name__)


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
        active_batch = await self._session.scalar(
            select(planning_batches.c.id).where(
                planning_batches.c.project_id == project_id,
                planning_batches.c.status.in_(ACTIVE_BATCH_STATUSES),
                planning_batches.c.effective_start_date <= planning_date,
                planning_batches.c.maximum_horizon_end >= planning_date,
            )
        )
        if active_batch is not None:
            raise PlanningRunInProgress(
                "Planning date is locked by an active batch",
                code="PLANNING_DATE_LOCKED_BY_BATCH",
            )
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
                int(row.id): int(row.available_units) for row in equipment_rows
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
        self,
        run_id: int,
        data: PlanningInput,
        result: PlanningResult,
        *,
        commit: bool = True,
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
                    {
                        "travel_time_seconds": result.travel_time_seconds_matrices,
                        "distance_meters": result.distance_matrices,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            if result.travel_time_seconds_matrices
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
                    total_distance_meters=route.total_distance_meters,
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
                            "distance_from_previous_meters": (
                                item.distance_from_previous_meters
                            ),
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
                travel_snapshot=_serialize_travel_snapshot(data.travel_snapshot),
                validation_errors=result.validation_errors,
                config_snapshot=_jsonable(
                    {
                        **asdict(data.config),
                        **result.objective_metrics,
                    }
                ),
                objective_range_snapshot=result.objective_metrics,
                fixed_active_engineer_count=result.objective_metrics.get(
                    "fixed_active_engineer_count", 0
                ),
                newly_activated_engineer_count=result.objective_metrics.get(
                    "newly_activated_engineer_count", 0
                ),
                used_engineer_count=result.objective_metrics.get(
                    "used_engineer_count", 0
                ),
                total_distance_meters=result.objective_metrics.get(
                    "total_distance_meters", 0
                ),
                max_engineer_distance_meters=result.objective_metrics.get(
                    "max_engineer_distance_meters", 0
                ),
            )
        )
        if status == PlanningRunStatus.SUCCESS.value and result.baseline_result:
            try:
                async with self._session.begin_nested():
                    await self._save_baseline(run_id, data, result)
            except Exception as error:
                # Baseline is an analytical sidecar. Its persistence must never
                # roll back an otherwise publishable optimized plan. Keep a
                # minimal marker so the retry scanner can recover this run.
                audit_context = {
                    "project_id": data.project_id,
                    "planning_run_id": run_id,
                    "planning_date": data.planning_date.isoformat(),
                    "plan_version": None,
                    "algorithm_version": result.baseline_result.algorithm_version,
                    "input_hash": result.baseline_result.input_hash,
                    "duration_ms": 0,
                    "jobs_count": len(data.baseline_jobs or data.jobs),
                    "engineers_count": len(data.engineers),
                }
                logger.error(
                    "baseline_persistence_failed",
                    extra={
                        **audit_context,
                        "error_type": type(error).__name__,
                    },
                )
                try:
                    async with self._session.begin_nested():
                        await self._save_baseline_failure_marker(run_id, data, result)
                except Exception as marker_error:
                    logger.error(
                        "baseline_failure_marker_persistence_failed",
                        extra={
                            **audit_context,
                            "error_type": type(marker_error).__name__,
                        },
                    )
        if commit:
            await self._session.commit()

    async def _save_baseline(
        self,
        run_id: int,
        data: PlanningInput,
        result: PlanningResult,
    ) -> None:
        baseline = result.baseline_result
        if baseline is None:
            return
        baseline_id = await self._session.scalar(
            insert(planning_baseline_results)
            .values(
                project_id=data.project_id,
                planning_run_id=run_id,
                planning_date=data.planning_date,
                status=baseline.status,
                algorithm_version=baseline.algorithm_version,
                snapshot_version="DAY_INPUT_V1",
                input_hash=baseline.input_hash,
                travel_matrix_hash=baseline.travel_matrix_hash,
                result_hash=baseline.result_hash,
                input_jobs_count=baseline.input_jobs_count,
                assigned_jobs_count=baseline.assigned_jobs_count,
                unassigned_jobs_count=baseline.unassigned_jobs_count,
                window_hit_count=baseline.window_hit_count,
                window_miss_count=baseline.window_miss_count,
                window_hit_rate=baseline.window_hit_rate,
                active_engineer_count=baseline.active_engineer_count,
                total_distance_meters=baseline.total_distance_meters,
                earliest_shift_start=baseline.earliest_shift_start,
                calculation_time_ms=baseline.calculation_time_ms,
                failure_code=baseline.failure_code,
                failure_message=baseline.failure_message,
                started_at=(
                    datetime.now(timezone.utc)
                    if baseline.status in {"READY", "FAILED"}
                    else None
                ),
                finished_at=(
                    datetime.now(timezone.utc)
                    if baseline.status
                    in {
                        "READY",
                        "FAILED",
                        "NOT_APPLICABLE_SHIFT_STARTED",
                    }
                    else None
                ),
                attempt_count=(0 if baseline.status == "PENDING" else 1),
                last_attempt_at=(
                    None if baseline.status == "PENDING" else datetime.now(timezone.utc)
                ),
                next_retry_at=(
                    datetime.now(timezone.utc) + timedelta(minutes=2)
                    if baseline.status == "FAILED"
                    else None
                ),
                result_payload=_jsonable(asdict(baseline)),
                distance_matrices=_jsonable(result.baseline_distance_matrices),
            )
            .returning(planning_baseline_results.c.id)
        )
        if baseline_id is None:
            raise RuntimeError("Failed to persist baseline result")
        await replace_baseline_audit_rows(
            self._session,
            baseline_result_id=int(baseline_id),
            project_id=data.project_id,
            baseline=baseline,
        )
        comparison = result.baseline_comparison
        if comparison is None:
            return
        await self._session.execute(
            insert(planning_plan_comparisons).values(
                project_id=data.project_id,
                planning_run_id=run_id,
                baseline_result_id=baseline_id,
                formula_version=comparison.formula_version,
                coverage_comparable=comparison.coverage_comparable,
                baseline_assigned_job_ids_hash=(
                    comparison.baseline_assigned_job_ids_hash
                ),
                optimized_assigned_job_ids_hash=(
                    comparison.optimized_assigned_job_ids_hash
                ),
                baseline_metrics=_jsonable(comparison.baseline_metrics),
                optimized_metrics=_jsonable(comparison.optimized_metrics),
                deltas=_jsonable(comparison.deltas),
                engineer_metrics=_jsonable(comparison.engineer_metrics),
            )
        )

    async def _save_baseline_failure_marker(
        self,
        run_id: int,
        data: PlanningInput,
        result: PlanningResult,
    ) -> None:
        baseline = result.baseline_result
        if baseline is None:
            return
        now = datetime.now(timezone.utc)
        not_applicable = baseline.status == "NOT_APPLICABLE_SHIFT_STARTED"
        status = "NOT_APPLICABLE_SHIFT_STARTED" if not_applicable else "FAILED"
        failure_code = None if not_applicable else "BASELINE_STORAGE_ERROR"
        failure_message = (
            None if not_applicable else "Не удалось сохранить контрольный результат"
        )
        payload = {
            **asdict(baseline),
            "status": status,
            "failure_code": failure_code,
            "failure_message": failure_message,
        }
        await self._session.execute(
            insert(planning_baseline_results).values(
                project_id=data.project_id,
                planning_run_id=run_id,
                planning_date=data.planning_date,
                status=status,
                algorithm_version=baseline.algorithm_version,
                snapshot_version="DAY_INPUT_V1",
                input_hash=baseline.input_hash,
                input_jobs_count=baseline.input_jobs_count,
                earliest_shift_start=baseline.earliest_shift_start,
                failure_code=failure_code,
                failure_message=failure_message,
                started_at=None if not_applicable else now,
                finished_at=now,
                attempt_count=0 if not_applicable else 1,
                last_attempt_at=None if not_applicable else now,
                next_retry_at=None if not_applicable else now + timedelta(minutes=2),
                result_payload=_jsonable(payload),
                distance_matrices={},
            )
        )

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


def _serialize_travel_snapshot(
    snapshot: dict[
        tuple[str, float, float, float, float], tuple[int | None, int | None]
    ],
) -> list[dict[str, Any]]:
    return [
        {
            "profile": key[0],
            "origin_latitude": key[1],
            "origin_longitude": key[2],
            "destination_latitude": key[3],
            "destination_longitude": key[4],
            "travel_time_seconds": value[0],
            "distance_meters": value[1],
        }
        for key, value in sorted(snapshot.items())
    ]


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        items = [_jsonable(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
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
