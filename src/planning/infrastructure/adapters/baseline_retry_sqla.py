from __future__ import annotations

import logging
import time as monotonic_time
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.errors import ConflictError, ObjectNotFoundError
from planning.application.services.baseline_fifo import (
    BaselineCalculationError,
    baseline_route_unavailable,
    calculate_fifo_baseline,
    compare_with_optimized,
    failed_result,
    required_baseline_travel_arcs,
)
from planning.domain.entities.coordinate import Coordinate
from planning.domain.entities.engineer import Engineer
from planning.domain.entities.job import Job
from planning.domain.entities.planning import (
    PlanningConfig,
    PlanningInput,
    PlanningResult,
    Route,
    RouteJob,
)
from planning.domain.enums import TransportType, WorkPriority
from planning.infrastructure.adapters.baseline_audit_persistence import (
    replace_baseline_audit_rows,
)
from planning.infrastructure.adapters.ortools_routing.travel import (
    prepare_travel_matrices_for_jobs,
)
from planning.infrastructure.adapters.planning_batch_repository_sqla import _jsonable
from planning.infrastructure.adapters.planning_run_repository_sqla import (
    _serialize_travel_snapshot,
)
from planning.infrastructure.adapters.travel_matrix_provider_factory import (
    TravelMatrixProviderFactory,
)
from planning.infrastructure.persistence_sqla.mappings.tables import (
    planning_baseline_results,
    planning_plan_comparisons,
    planning_route_jobs,
    planning_routes,
    planning_runs,
)

logger = logging.getLogger(__name__)

MAX_BASELINE_ATTEMPTS = 3
RETRY_DELAY = timedelta(minutes=2)


class SqlaBaselineRetryOperations:
    """Rebuild FIFO from the immutable run snapshot, never from live entities."""

    def __init__(
        self,
        session: AsyncSession,
        matrix_factory: TravelMatrixProviderFactory,
    ):
        self._session = session
        self._matrix_factory = matrix_factory

    async def retry(self, project_id: int, planning_run_id: int) -> dict[str, Any]:
        return await self._execute(project_id, planning_run_id, allow_pending=False)

    async def run_queued(self, project_id: int, planning_run_id: int) -> dict[str, Any]:
        return await self._execute(project_id, planning_run_id, allow_pending=True)

    async def _execute(
        self,
        project_id: int,
        planning_run_id: int,
        *,
        allow_pending: bool,
    ) -> dict[str, Any]:
        context = await self._claim(
            project_id, planning_run_id, allow_pending=allow_pending
        )
        if context["status"] != "RUNNING" or context.get("reused"):
            return context

        started = monotonic_time.perf_counter()
        stage = "calculation"
        log_context: dict[str, Any] = {
            "project_id": project_id,
            "planning_run_id": planning_run_id,
            "planning_date": str(context.get("planning_date") or ""),
            "plan_version": context.get("plan_version_id"),
            "algorithm_version": context.get("algorithm_version"),
            "input_hash": context.get("expected_input_hash"),
            "jobs_count": int(context.get("input_jobs_count") or 0),
            "engineers_count": 0,
            "attempt_count": context.get("attempt_count"),
        }
        try:
            data = _planning_input(context)
            log_context.update(
                planning_date=data.planning_date.isoformat(),
                jobs_count=len(data.baseline_jobs),
                engineers_count=len(data.engineers),
            )
            logger.info(
                "baseline_calculation_started",
                extra={**log_context, "duration_ms": 0},
            )
            optimized = await self._optimized_result(planning_run_id, context)
            required_arcs = required_baseline_travel_arcs(data)
            provider = self._matrix_factory.create(data.config.travel_provider)
            travel = await prepare_travel_matrices_for_jobs(
                data,
                data.baseline_jobs,
                provider,
                required_arcs=required_arcs,
            )
            if baseline_route_unavailable(data.travel_snapshot, required_arcs):
                raise BaselineCalculationError(
                    "BASELINE_ROUTE_UNAVAILABLE",
                    "Required FIFO arc is unavailable in the captured travel snapshot",
                )
            logger.info(
                "baseline_distance_calculated",
                extra={
                    **log_context,
                    "duration_ms": int(
                        (monotonic_time.perf_counter() - started) * 1000
                    ),
                },
            )
            baseline = calculate_fifo_baseline(data, travel.meters)
            logger.info(
                "baseline_assignment_completed",
                extra={
                    **log_context,
                    "duration_ms": baseline.calculation_time_ms,
                    "assigned_jobs_count": baseline.assigned_jobs_count,
                    "active_engineer_count": baseline.active_engineer_count,
                },
            )
            if baseline.input_hash != context["expected_input_hash"]:
                raise BaselineCalculationError(
                    "BASELINE_INPUT_HASH_MISMATCH",
                    "Сохранённый снимок не совпадает с исходной попыткой",
                )
            comparison = compare_with_optimized(data, optimized, baseline)
            if comparison is None:
                raise BaselineCalculationError(
                    "BASELINE_VALIDATION_FAILED",
                    "Не удалось построить сравнение результатов",
                )
            logger.info(
                "baseline_validation_completed",
                extra={
                    **log_context,
                    "duration_ms": int(
                        (monotonic_time.perf_counter() - started) * 1000
                    ),
                    "coverage_comparable": comparison.coverage_comparable,
                },
            )
            stage = "storage"
            await self._save_ready(
                int(context["baseline_id"]),
                planning_run_id,
                data,
                baseline,
                comparison,
                travel.meters,
            )
            stage = "complete"
            logger.info(
                "baseline_calculation_ready",
                extra={
                    **log_context,
                    "duration_ms": int(
                        (monotonic_time.perf_counter() - started) * 1000
                    ),
                    "result_hash": baseline.result_hash,
                },
            )
            return {
                "planning_run_id": planning_run_id,
                "status": "READY",
                "attempt_count": context["attempt_count"],
                "coverage_comparable": comparison.coverage_comparable,
            }
        except BaselineCalculationError as error:
            code = error.code
            message = _safe_failure_message(code)
            logger.warning(
                "baseline_calculation_failed",
                extra={
                    **log_context,
                    "failure_code": code,
                    "duration_ms": int(
                        (monotonic_time.perf_counter() - started) * 1000
                    ),
                },
            )
        except Exception as error:
            detail = str(error).upper()
            code = (
                "BASELINE_STORAGE_ERROR"
                if stage == "storage"
                else "BASELINE_DISTANCE_DATA_NOT_READY"
                if "DISTANCE_DATA_NOT_READY" in detail
                or "INVALID_TRAVEL_MATRIX" in detail
                else "BASELINE_TRAVEL_PROVIDER_UNAVAILABLE"
                if "TRAVEL_PROVIDER_UNAVAILABLE" in detail
                else "BASELINE_CALCULATION_FAILED"
            )
            message = _safe_failure_message(code)
            logger.warning(
                "baseline_calculation_failed",
                extra={
                    **log_context,
                    "failure_code": code,
                    "error_type": type(error).__name__,
                    "duration_ms": int(
                        (monotonic_time.perf_counter() - started) * 1000
                    ),
                },
            )
        if stage == "storage":
            # A failed flush leaves the SQLAlchemy transaction unusable. Roll
            # back only this analytical attempt before persisting its safe
            # FAILED state; the optimized run was committed independently.
            await self._session.rollback()
        data = _planning_input(context)
        failure = failed_result(data, code, message, started_at=started)
        await self._save_failed(
            int(context["baseline_id"]),
            planning_run_id,
            failure,
            int(context["attempt_count"]),
        )
        return {
            "planning_run_id": planning_run_id,
            "status": "FAILED",
            "attempt_count": context["attempt_count"],
            "failure_code": code,
            "failure_message": message,
            "next_retry_at": (
                datetime.now(timezone.utc) + RETRY_DELAY
                if code != "BASELINE_ROUTE_UNAVAILABLE"
                and int(context["attempt_count"]) < MAX_BASELINE_ATTEMPTS
                else None
            ),
        }

    async def _claim(
        self,
        project_id: int,
        planning_run_id: int,
        *,
        allow_pending: bool,
    ) -> dict[str, Any]:
        row = (
            (
                await self._session.execute(
                    select(
                        planning_baseline_results.c.id.label("baseline_id"),
                        planning_baseline_results.c.status,
                        planning_baseline_results.c.attempt_count,
                        planning_baseline_results.c.input_hash.label(
                            "expected_input_hash"
                        ),
                        planning_baseline_results.c.plan_version_id,
                        planning_baseline_results.c.algorithm_version,
                        planning_runs.c.planning_date,
                        planning_runs.c.timezone,
                        planning_runs.c.status.label("planning_run_status"),
                        planning_runs.c.config_snapshot,
                        planning_runs.c.input_snapshot,
                        planning_runs.c.travel_snapshot,
                        planning_runs.c.input_jobs_count,
                        planning_runs.c.objective,
                        planning_runs.c.drop_cost,
                        planning_runs.c.travel_cost,
                        planning_runs.c.solver_status,
                        planning_runs.c.solver_time_ms,
                        planning_runs.c.used_engineer_count,
                        planning_runs.c.total_distance_meters,
                    )
                    .select_from(
                        planning_baseline_results.join(
                            planning_runs,
                            planning_runs.c.id
                            == planning_baseline_results.c.planning_run_id,
                        )
                    )
                    .where(
                        planning_baseline_results.c.project_id == project_id,
                        planning_baseline_results.c.planning_run_id == planning_run_id,
                    )
                    .with_for_update(of=planning_baseline_results)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ObjectNotFoundError("Базовый результат не найден")
        values = dict(row)
        if values["planning_run_status"] != "SUCCESS":
            raise ConflictError(
                "Оптимальный результат этого запуска не опубликован",
                code="BASELINE_RETRY_RUN_NOT_SUCCESSFUL",
            )
        if values["status"] in {"READY", "RUNNING"}:
            await self._session.commit()
            return {
                "planning_run_id": planning_run_id,
                "status": values["status"],
                "attempt_count": int(values["attempt_count"] or 0),
                "reused": True,
            }
        if values["status"] == "PENDING" and not allow_pending:
            raise ConflictError(
                "Базовый расчёт ещё ожидает выполнения",
                code="BASELINE_RETRY_NOT_ALLOWED",
            )
        if values["status"] not in {"PENDING", "FAILED"}:
            raise ConflictError(
                "Повтор доступен только для неуспешного базового расчёта",
                code="BASELINE_RETRY_NOT_ALLOWED",
            )
        previous_attempts = int(values["attempt_count"] or 0)
        if previous_attempts >= MAX_BASELINE_ATTEMPTS:
            raise ConflictError(
                "Лимит повторов базового расчёта исчерпан",
                code="BASELINE_RETRY_LIMIT_EXCEEDED",
            )
        attempt_count = previous_attempts + 1
        now = datetime.now(timezone.utc)
        await self._session.execute(
            update(planning_baseline_results)
            .where(planning_baseline_results.c.id == values["baseline_id"])
            .values(
                status="RUNNING",
                attempt_count=attempt_count,
                last_attempt_at=now,
                next_retry_at=None,
                failure_code=None,
                failure_message=None,
                started_at=now,
                finished_at=None,
            )
        )
        await self._session.commit()
        return {
            **values,
            "status": "RUNNING",
            "attempt_count": attempt_count,
            "reused": False,
        }

    async def _optimized_result(
        self, planning_run_id: int, context: dict[str, Any]
    ) -> PlanningResult:
        route_rows = (
            (
                await self._session.execute(
                    select(planning_routes)
                    .where(planning_routes.c.planning_run_id == planning_run_id)
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
                    .where(planning_route_jobs.c.planning_run_id == planning_run_id)
                    .order_by(
                        planning_route_jobs.c.planning_route_id,
                        planning_route_jobs.c.sequence,
                    )
                )
            )
            .mappings()
            .all()
        )
        snapshot_jobs = {
            int(item["id"]): item
            for item in (context.get("input_snapshot") or {}).get("jobs", [])
        }
        grouped: dict[int, list[RouteJob]] = {}
        for row in job_rows:
            snapshot_job = snapshot_jobs.get(int(row.job_id), {})
            grouped.setdefault(int(row.planning_route_id), []).append(
                RouteJob(
                    job_id=int(row.job_id),
                    sequence=int(row.sequence),
                    planned_arrival=row.planned_arrival,
                    planned_start=row.planned_start,
                    planned_finish=row.planned_finish,
                    travel_from_previous_min=int(row.travel_from_previous_min),
                    waiting_before_job_min=int(row.waiting_before_job_min),
                    drop_penalty=int(snapshot_job.get("drop_penalty") or 0),
                    distance_from_previous_meters=int(
                        row.distance_from_previous_meters or 0
                    ),
                )
            )
        routes = [
            Route(
                engineer_id=int(row.engineer_id),
                planned_start=row.planned_start,
                planned_finish=row.planned_finish,
                total_travel_min=int(row.total_travel_min),
                total_service_min=int(row.total_service_min),
                total_waiting_min=int(row.total_waiting_min),
                jobs=grouped.get(int(row.id), []),
                equipment_type_ids=set(),
                total_distance_meters=int(row.total_distance_meters or 0),
            )
            for row in route_rows
        ]
        return PlanningResult(
            routes=routes,
            unassigned=[],
            solver_status=str(context.get("solver_status") or "SUCCESS"),
            objective=int(context.get("objective") or 0),
            drop_cost=int(context.get("drop_cost") or 0),
            travel_cost=int(context.get("travel_cost") or 0),
            solver_time_ms=int(context.get("solver_time_ms") or 0),
            objective_metrics={
                "used_engineer_count": int(
                    context.get("used_engineer_count") or len(routes)
                ),
                "total_distance_meters": int(
                    context.get("total_distance_meters")
                    or sum(route.total_distance_meters for route in routes)
                ),
            },
        )

    async def _save_ready(
        self,
        baseline_id: int,
        planning_run_id: int,
        data: PlanningInput,
        baseline: Any,
        comparison: Any,
        matrices: dict[str, list[list[int | None]]],
    ) -> None:
        # Publication holds the project row before updating the baseline's
        # plan_version_id. Insert dependent audit rows first (they acquire the
        # same project's FK key-share lock), then update the baseline row.
        # Taking the baseline row lock first creates a real PostgreSQL deadlock.
        await replace_baseline_audit_rows(
            self._session,
            baseline_result_id=baseline_id,
            project_id=data.project_id,
            baseline=baseline,
        )
        await self._session.execute(
            delete(planning_plan_comparisons).where(
                planning_plan_comparisons.c.planning_run_id == planning_run_id
            )
        )
        await self._session.execute(
            insert(planning_plan_comparisons).values(
                project_id=data.project_id,
                planning_run_id=planning_run_id,
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
        # The FIFO route may need arcs that the optimizer never traversed or
        # queried (for example when its input was limited). Freeze those arcs
        # with the same run before READY so a later replay never asks the road
        # provider for a potentially different distance.
        await self._session.execute(
            update(planning_runs)
            .where(
                planning_runs.c.id == planning_run_id,
                planning_runs.c.project_id == data.project_id,
            )
            .values(travel_snapshot=_serialize_travel_snapshot(data.travel_snapshot))
        )
        await self._session.execute(
            update(planning_baseline_results)
            .where(planning_baseline_results.c.id == baseline_id)
            .values(
                status="READY",
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
                failure_code=None,
                failure_message=None,
                finished_at=datetime.now(timezone.utc),
                next_retry_at=None,
                result_payload=_jsonable(asdict(baseline)),
                distance_matrices=_jsonable(matrices),
            )
        )
        await self._session.commit()

    async def _save_failed(
        self,
        baseline_id: int,
        planning_run_id: int,
        failure: Any,
        attempt_count: int,
    ) -> None:
        await self._session.execute(
            delete(planning_plan_comparisons).where(
                planning_plan_comparisons.c.planning_run_id == planning_run_id
            )
        )
        await replace_baseline_audit_rows(
            self._session,
            baseline_result_id=baseline_id,
            project_id=failure.project_id,
            baseline=failure,
        )
        await self._session.execute(
            update(planning_baseline_results)
            .where(planning_baseline_results.c.id == baseline_id)
            .values(
                status="FAILED",
                travel_matrix_hash=None,
                result_hash=None,
                assigned_jobs_count=0,
                unassigned_jobs_count=0,
                window_hit_count=0,
                window_miss_count=0,
                window_hit_rate=None,
                active_engineer_count=0,
                total_distance_meters=0,
                calculation_time_ms=failure.calculation_time_ms,
                failure_code=failure.failure_code,
                failure_message=failure.failure_message,
                finished_at=datetime.now(timezone.utc),
                next_retry_at=(
                    datetime.now(timezone.utc) + RETRY_DELAY
                    if failure.failure_code != "BASELINE_ROUTE_UNAVAILABLE"
                    and attempt_count < MAX_BASELINE_ATTEMPTS
                    else None
                ),
                result_payload=_jsonable(asdict(failure)),
                distance_matrices={},
            )
        )
        await self._session.commit()


def _planning_input(context: dict[str, Any]) -> PlanningInput:
    snapshot = dict(context.get("input_snapshot") or {})
    config_snapshot = context.get("config_snapshot") or {}
    config = PlanningConfig(
        **{
            name: config_snapshot[name]
            for name in PlanningConfig.__dataclass_fields__
            if name in config_snapshot
        }
    )
    jobs = [_job(item) for item in snapshot.get("jobs", [])]
    engineers = [_engineer(item) for item in snapshot.get("engineers", [])]
    planning_date = _date(context["planning_date"])
    return PlanningInput(
        project_id=int(snapshot.get("project_id") or 0),
        planning_date=planning_date,
        timezone=str(context["timezone"]),
        config=config,
        jobs=jobs,
        engineers=engineers,
        equipment_units={
            int(key): int(value)
            for key, value in (snapshot.get("equipment_units") or {}).items()
        },
        pre_unassigned=[],
        input_jobs_count=int(context.get("input_jobs_count") or len(jobs)),
        sla_critical_job_ids=frozenset(
            job.id for job in jobs if job.sla_date <= planning_date
        ),
        snapshot=snapshot,
        baseline_jobs=jobs,
        travel_snapshot=_deserialize_travel_snapshot(
            context.get("travel_snapshot") or []
        ),
    )


def _job(value: dict[str, Any]) -> Job:
    coordinate = value.get("coordinate") or {}
    required_transport = value.get("required_transport")
    allowed = value.get("allowed_engineer_ids")
    return Job(
        id=int(value["id"]),
        sla_date=_date(value["sla_date"]),
        duration_min=int(value["duration_min"]),
        coordinate=Coordinate(
            float(coordinate["latitude"]), float(coordinate["longitude"])
        ),
        window_start_min=int(value["window_start_min"]),
        window_end_min=int(value["window_end_min"]),
        required_transport=(
            TransportType(str(required_transport)) if required_transport else None
        ),
        required_qualifications=frozenset(
            int(item) for item in value.get("required_qualifications", [])
        ),
        required_equipment=frozenset(
            int(item) for item in value.get("required_equipment", [])
        ),
        created_at=_datetime(value["created_at"]),
        drop_penalty=int(value.get("drop_penalty") or 0),
        allowed_engineer_ids=(
            frozenset(int(item) for item in allowed) if allowed is not None else None
        ),
        mandatory=bool(value.get("mandatory", False)),
        priority=WorkPriority(str(value.get("priority", "LOW"))),
        received_at=_datetime(value.get("received_at") or value["created_at"]),
        ingest_sequence=int(value.get("ingest_sequence") or 1),
    )


def _engineer(value: dict[str, Any]) -> Engineer:
    coordinate = value["coordinate"]
    return Engineer(
        id=int(value["id"]),
        transport_type=TransportType(str(value["transport_type"])),
        coordinate=Coordinate(
            float(coordinate["latitude"]), float(coordinate["longitude"])
        ),
        shift_start_min=int(value["shift_start_min"]),
        shift_end_min=int(value["shift_end_min"]),
        qualifications=frozenset(int(item) for item in value["qualifications"]),
        name=value.get("name"),
        created_at=(
            _datetime(value["created_at"]) if value.get("created_at") else None
        ),
    )


def _datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _safe_failure_message(code: str) -> str:
    return {
        "BASELINE_INPUT_HASH_MISMATCH": (
            "Сохранённый снимок не прошёл проверку целостности"
        ),
        "BASELINE_DISTANCE_DATA_NOT_READY": (
            "Дорожные расстояния для контрольного плана ещё не готовы"
        ),
        "BASELINE_ROUTE_UNAVAILABLE": (
            "Нужный участок контрольного маршрута отсутствует "
            "в сохранённом дорожном снимке"
        ),
        "BASELINE_TRAVEL_PROVIDER_UNAVAILABLE": ("Дорожный сервис временно недоступен"),
        "BASELINE_VALIDATION_FAILED": (
            "Контрольный результат не прошёл проверку целостности"
        ),
        "BASELINE_INVALID_SHIFT": "Снимок содержит некорректную смену",
        "BASELINE_INVALID_DURATION": (
            "Снимок содержит некорректную длительность работ"
        ),
        "BASELINE_STORAGE_ERROR": ("Не удалось сохранить контрольный результат"),
        "BASELINE_CALCULATION_FAILED": (
            "Контрольный расчёт завершился технической ошибкой"
        ),
        "BASELINE_PROCESS_RESTARTED": (
            "Контрольный расчёт был прерван и поставлен в очередь повторно"
        ),
    }.get(code, "Контрольный расчёт временно недоступен")


def _deserialize_travel_snapshot(
    values: list[dict[str, Any]],
) -> dict[tuple[str, float, float, float, float], tuple[int | None, int | None]]:
    return {
        (
            str(item["profile"]),
            float(item["origin_latitude"]),
            float(item["origin_longitude"]),
            float(item["destination_latitude"]),
            float(item["destination_longitude"]),
        ): (
            item.get("travel_time_seconds"),
            item.get("distance_meters"),
        )
        for item in values
    }
