import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from time import monotonic
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from planning.application.services.dynamic_today_planning import (
    CandidateComparisonTimeout,
    DynamicTodayPlanningService,
)
from planning.application.services.multi_day_planning import MultiDayPlanningService
from planning.application.services.planning_input_normalizer import (
    PlanningInputNormalizer,
)
from planning.application.validators.dynamic_plan import DynamicPlanValidator
from planning.application.validators.planning_batch import PlanningBatchValidator
from planning.application.validators.planning_result import PlanningValidator
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.dynamic_planning_repository_sqla import (
    SqlaDynamicPlanningRepository,
    StaleDynamicSnapshot,
)
from planning.infrastructure.adapters.geocoder_factory import create_geocoder
from planning.infrastructure.adapters.planning_batch_repository_sqla import (
    SqlaPlanningBatchRepository,
)
from planning.infrastructure.adapters.planning_solver_ortools import (
    OrToolsPlanningSolverFactory,
)
from planning.infrastructure.adapters.travel_matrix_provider_factory import (
    TravelMatrixProviderFactory,
)
from planning.infrastructure.adapters.travel_matrix_provider_static import (
    StaticTravelMatrixProvider,
)
from planning.infrastructure.adapters.travel_matrix_provider_valhalla import (
    ValhallaTravelMatrixProvider,
)
from planning.infrastructure.persistence_sqla.mappings.tables import (
    planning_batch_days,
    planning_batches,
    planning_config,
    planning_events,
    planning_runs,
    projects,
)

logger = logging.getLogger(__name__)


def _seven_day_block_start(anchor: date, affected: date) -> date:
    offset = max(0, (affected - anchor).days)
    return anchor + timedelta(days=(offset // 7) * 7)


def _as_event_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class InProcessPlanningBatchExecutor:
    """Runs a durable database-backed batch outside the request-scoped session.

    The database is the source of truth for status and progress. Keeping only task
    handles in memory prevents duplicate scheduling inside one process; a CREATED or
    RUNNING row can safely be scheduled again by operational recovery code.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        config: PlanningServiceConfig,
    ):
        self._sessionmaker = sessionmaker
        engine = sessionmaker.kw.get("bind")
        if not isinstance(engine, AsyncEngine):
            raise TypeError(
                "Planning executor requires an AsyncEngine-bound sessionmaker"
            )
        self._engine = engine
        self._config = config
        self._tasks: dict[int, asyncio.Task] = {}
        self._project_tasks: dict[int, asyncio.Task] = {}
        self._nightly_task: asyncio.Task | None = None
        self._event_scan_task: asyncio.Task | None = None

    def schedule(self, batch_id: int) -> None:
        current = self._tasks.get(batch_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._run(batch_id), name=f"planning-batch-{batch_id}"
        )
        self._tasks[batch_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(batch_id, None))

    def schedule_project(self, project_id: int) -> None:
        current = self._project_tasks.get(project_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._run_project(project_id), name=f"dynamic-planning-project-{project_id}"
        )
        self._project_tasks[project_id] = task
        task.add_done_callback(lambda _: self._project_tasks.pop(project_id, None))

    async def recover(self) -> None:
        async with self._sessionmaker() as session:
            dynamic_project_ids = [
                int(value)
                for value in (
                    await session.scalars(
                        select(planning_events.c.project_id)
                        .where(planning_events.c.state.in_(("PENDING", "RUNNING")))
                        .distinct()
                    )
                ).all()
            ]
        for project_id in dynamic_project_ids:
            async with self._advisory_lock(1_397_244_753, project_id) as locked:
                if not locked:
                    continue
                async with self._sessionmaker() as session:
                    interrupted_batch_ids = [
                        int(value)
                        for value in (
                            await session.scalars(
                                select(planning_batches.c.id).where(
                                    planning_batches.c.project_id == project_id,
                                    planning_batches.c.idempotency_key.like(
                                        "dynamic:%"
                                    ),
                                    planning_batches.c.status.in_(
                                        (
                                            "CREATED",
                                            "PREPARING",
                                            "RUNNING",
                                            "STOP_REQUESTED",
                                        )
                                    ),
                                )
                            )
                        ).all()
                    ]
                    if interrupted_batch_ids:
                        await session.execute(
                            update(planning_runs)
                            .where(
                                planning_runs.c.planning_batch_id.in_(
                                    interrupted_batch_ids
                                ),
                                planning_runs.c.status.in_(
                                    ("CREATED", "PREPARING", "RUNNING")
                                ),
                            )
                            .values(
                                status="FAILED",
                                error_code="PROCESS_RESTARTED",
                                error_message="Interrupted before atomic publication",
                            )
                        )
                        await session.execute(
                            update(planning_batch_days)
                            .where(
                                planning_batch_days.c.planning_batch_id.in_(
                                    interrupted_batch_ids
                                ),
                                planning_batch_days.c.status == "RUNNING",
                            )
                            .values(status="FAILED", error_code="PROCESS_RESTARTED")
                        )
                        await session.execute(
                            update(planning_batches)
                            .where(planning_batches.c.id.in_(interrupted_batch_ids))
                            .values(
                                status="FAILED",
                                completion_reason="DAY_RUN_FAILED",
                                current_flag=False,
                                error_code="PROCESS_RESTARTED",
                                error_message="Interrupted before atomic publication",
                                finished_at=datetime.now(timezone.utc),
                            )
                        )
                    await session.execute(
                        update(planning_events)
                        .where(
                            planning_events.c.project_id == project_id,
                            planning_events.c.state == "RUNNING",
                        )
                        .values(state="PENDING", error_code="PROCESS_RESTARTED")
                    )
                    await session.commit()
        async with self._sessionmaker() as session:
            batch_ids = [
                int(value)
                for value in (
                    await session.scalars(
                        select(planning_batches.c.id).where(
                            planning_batches.c.status.in_(
                                (
                                    "CREATED",
                                    "PREPARING",
                                    "RUNNING",
                                    "STOP_REQUESTED",
                                )
                            ),
                            planning_batches.c.idempotency_key.not_like("dynamic:%"),
                        )
                    )
                ).all()
            ]
            project_ids = [
                int(value)
                for value in (
                    await session.scalars(
                        select(planning_events.c.project_id)
                        .where(planning_events.c.state == "PENDING")
                        .distinct()
                    )
                ).all()
            ]
        for batch_id in batch_ids:
            self.schedule(batch_id)
        for project_id in project_ids:
            self.schedule_project(project_id)
        if self._nightly_task is None or self._nightly_task.done():
            self._nightly_task = asyncio.create_task(
                self._nightly_loop(), name="dynamic-planning-nightly-scheduler"
            )
        if self._event_scan_task is None or self._event_scan_task.done():
            self._event_scan_task = asyncio.create_task(
                self._event_scan_loop(), name="dynamic-planning-event-scanner"
            )

    async def shutdown(self) -> None:
        if self._nightly_task is not None:
            self._nightly_task.cancel()
        if self._event_scan_task is not None:
            self._event_scan_task.cancel()
        tasks = [*self._tasks.values(), *self._project_tasks.values()]
        tasks.extend(
            task
            for task in (self._nightly_task, self._event_scan_task)
            if task is not None
        )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _nightly_loop(self) -> None:
        while True:
            try:
                await self._enqueue_due_nightly_events()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("nightly_planning_scheduler_failed")
            await asyncio.sleep(30)

    async def _event_scan_loop(self) -> None:
        """Close the small cross-process race between unlock and a new event."""
        while True:
            try:
                async with self._sessionmaker() as session:
                    project_ids = [
                        int(value)
                        for value in (
                            await session.scalars(
                                select(planning_events.c.project_id)
                                .where(planning_events.c.state == "PENDING")
                                .distinct()
                            )
                        ).all()
                    ]
                for project_id in project_ids:
                    self.schedule_project(project_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("dynamic_planning_event_scan_failed")
            await asyncio.sleep(1)

    async def _enqueue_due_nightly_events(self) -> None:
        scheduled_project_ids: set[int] = set()
        async with self._sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(
                            projects.c.id,
                            projects.c.planning_timezone,
                            planning_config.c.nightly_planning_time,
                        )
                        .join(
                            planning_config,
                            and_(
                                planning_config.c.project_id == projects.c.id,
                                planning_config.c.active.is_(True),
                            ),
                        )
                        .where(
                            projects.c.status == "ACTIVE",
                            planning_config.c.nightly_planning_enabled.is_(True),
                        )
                    )
                )
                .mappings()
                .all()
            )
            repository = SqlaDynamicPlanningRepository(session)
            for row in rows:
                local = datetime.now(timezone.utc).astimezone(
                    ZoneInfo(row.planning_timezone)
                )
                if local.time().replace(tzinfo=None) < row.nightly_planning_time:
                    continue
                event = await repository.enqueue(
                    int(row.id),
                    "NIGHTLY",
                    None,
                    f"nightly:{row.id}:{local.date().isoformat()}",
                )
                if event["state"] == "PENDING":
                    scheduled_project_ids.add(int(row.id))
            await session.commit()
        for project_id in scheduled_project_ids:
            self.schedule_project(project_id)

    async def _run_project(self, project_id: int) -> None:
        async with self._advisory_lock(1_397_244_753, project_id) as locked:
            if not locked:
                return
            async with self._sessionmaker() as session:
                repository = SqlaDynamicPlanningRepository(session)
                config = await SqlaPlanningBatchRepository(session)._active_config(
                    project_id
                )
                if config is None:
                    return
                while events := await repository.claim_pending(
                    project_id,
                    coalesce_window_sec=int(config.event_coalesce_window_sec),
                    coalesce_max_wait_sec=int(config.event_coalesce_max_wait_sec),
                ):
                    event_ids = [int(item["id"]) for item in events]
                    try:
                        await self._process_events(
                            session, repository, project_id, events
                        )
                    except StaleDynamicSnapshot as error:
                        retry = (
                            max(int(item.get("attempt_count") or 0) for item in events)
                            < 2
                        )
                        await repository.fail(
                            event_ids, "STALE_SNAPSHOT", str(error), retry=retry
                        )
                    except CandidateComparisonTimeout as error:
                        logger.warning(
                            "candidate_comparison_timeout project_id=%s events=%s",
                            project_id,
                            event_ids,
                        )
                        retry = (
                            max(int(item.get("attempt_count") or 0) for item in events)
                            < 2
                        )
                        await repository.fail(
                            event_ids,
                            "CANDIDATE_COMPARISON_TIMEOUT",
                            str(error),
                            retry=retry,
                        )
                    except Exception as error:
                        logger.exception(
                            "dynamic_planning_failed project_id=%s events=%s",
                            project_id,
                            event_ids,
                        )
                        retry = (
                            max(int(item.get("attempt_count") or 0) for item in events)
                            < 2
                        )
                        message = str(error)
                        code = next(
                            (
                                value
                                for value in (
                                    "DISTANCE_DATA_NOT_READY",
                                    "TRAVEL_PROVIDER_UNAVAILABLE",
                                    "INVALID_PENALTY_BANDS",
                                    "OBJECTIVE_RANGE_OVERFLOW",
                                    "FAILED_VALIDATION",
                                )
                                if value in message
                            ),
                            "SYSTEM_ERROR",
                        )
                        await repository.fail(
                            event_ids,
                            code,
                            message,
                            retry=retry
                            and code
                            in {
                                "DISTANCE_DATA_NOT_READY",
                                "TRAVEL_PROVIDER_UNAVAILABLE",
                            },
                        )

    async def _process_events(
        self,
        session: AsyncSession,
        repository: SqlaDynamicPlanningRepository,
        project_id: int,
        events: list[dict],
    ) -> None:
        event_started = monotonic()
        timezone_name = await SqlaPlanningBatchRepository(session).get_project_timezone(
            project_id
        )
        planning_date = (
            datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
        )
        context = await repository.load_context(project_id, planning_date)
        event_limit = int(context["source"]["config"].get("event_time_limit_sec", 1200))
        context["event_deadline_monotonic"] = event_started + event_limit
        event_id_by_job: dict[int, int] = {}
        for event in events:
            if event.get("event_type") not in {
                "JOB_CREATED",
                "IMPORT",
                "JOBS_IMPORTED",
            }:
                continue
            for job_id in event.get("job_ids") or []:
                event_id_by_job[int(job_id)] = int(event["id"])
        jobs_by_id = {int(item["id"]): item for item in context["source"]["jobs"]}
        due_today = {
            job_id: event_id
            for job_id, event_id in event_id_by_job.items()
            if job_id in jobs_by_id
            and date.fromisoformat(jobs_by_id[job_id]["sla_date"]) <= planning_date
        }
        geocoder = create_geocoder(session, self._config)
        matrix_factory = TravelMatrixProviderFactory(
            StaticTravelMatrixProvider(),
            ValhallaTravelMatrixProvider(session, self._config),
        )
        today_service = DynamicTodayPlanningService(
            PlanningInputNormalizer(geocoder),
            OrToolsPlanningSolverFactory(matrix_factory),
            PlanningValidator(),
            repository,
        )
        trigger_types = {str(item["event_type"]) for item in events}
        cancelled_job_ids = {
            int(job_id)
            for item in events
            if item["event_type"] == "JOB_CANCELLED"
            for job_id in item.get("job_ids") or []
        }
        cancelled_at_by_job = {
            int(job_id): _as_event_datetime(
                (item.get("event_payload") or {}).get("cancelled_at")
            )
            for item in events
            if item["event_type"] == "JOB_CANCELLED"
            for job_id in item.get("job_ids") or []
            if (item.get("event_payload") or {}).get("cancelled_at")
        }
        planning_date_text = planning_date.isoformat()
        block_anchor = context.get("block_anchor_date") or planning_date
        if isinstance(block_anchor, str):
            block_anchor = date.fromisoformat(block_anchor)
        current_block_start = _seven_day_block_start(block_anchor, planning_date)
        lost_engineer_ids = {
            int(engineer_id)
            for item in events
            if item["event_type"]
            in {"ENGINEER_AVAILABILITY_LOST", "ENGINEER_AVAILABILITY_RESTORED"}
            and planning_date_text
            in (item.get("event_payload") or {}).get("lost_dates", [])
            for engineer_id in item.get("engineer_ids") or []
        }
        restored_engineer_ids = {
            int(engineer_id)
            for item in events
            if item["event_type"]
            in {"ENGINEER_AVAILABILITY_LOST", "ENGINEER_AVAILABILITY_RESTORED"}
            and planning_date_text
            in (item.get("event_payload") or {}).get("restored_dates", [])
            for engineer_id in item.get("engineer_ids") or []
        }
        current_block_end = current_block_start + timedelta(days=6)
        cancelled_current_block_engineers = {
            int(item["engineer_id"])
            for item in context["current_assignments"]
            if int(item["job_id"]) in cancelled_job_ids
            and current_block_start
            <= date.fromisoformat(str(item["planning_date"]))
            <= current_block_end
        }
        full_replan = any(
            item["event_type"] in {"MANUAL", "NIGHTLY"} for item in events
        ) or bool(
            lost_engineer_ids
            or cancelled_current_block_engineers
            or restored_engineer_ids
        )
        if full_replan:
            today_assignments = await today_service.replan_full_today(
                project_id=project_id,
                planning_date=planning_date,
                context=context,
                # New jobs from coalesced create/import events must enter today only
                # through the due-today candidate branch below. Otherwise a full
                # manual/nightly solve can assign the same job before the
                # per-engineer comparison and create a duplicate assignment.
                excluded_job_ids=set(event_id_by_job),
                cancelled_job_ids=cancelled_job_ids,
                cancelled_at_by_job=cancelled_at_by_job,
                unavailable_engineer_ids=lost_engineer_ids,
                affected_engineer_ids=(
                    cancelled_current_block_engineers
                    if cancelled_current_block_engineers and not lost_engineer_ids
                    else None
                ),
                restoration_engineer_ids=restored_engineer_ids,
            )
            context = {**context, "today_assignments": today_assignments}
        else:
            today_assignments = context["today_assignments"]
        if due_today:
            today_assignments, _ = await today_service.insert_due_jobs(
                project_id=project_id,
                event_id_by_job=due_today,
                planning_date=planning_date,
                context=context,
            )
        if cancelled_job_ids or lost_engineer_ids or restored_engineer_ids:
            today_validation_errors = DynamicPlanValidator().validate_publication(
                today_assignments,
                timezone_name=timezone_name,
                minimum_date=planning_date,
                maximum_date=planning_date,
            )
            if cancelled_job_ids & {int(item["job_id"]) for item in today_assignments}:
                today_validation_errors.append("cancelled job remains assigned")
        else:
            today_validation_errors = DynamicPlanValidator().validate_today(
                context, today_assignments, planning_date
            )
        if today_validation_errors:
            raise RuntimeError(
                "FAILED_VALIDATION: " + "; ".join(today_validation_errors)
            )
        today_job_ids = {
            int(item["job_id"])
            for item in today_assignments
            if item.get("status") == "NEW"
        }
        affected_future_dates = [
            date.fromisoformat(str(item["planning_date"]))
            for item in context["current_assignments"]
            if int(item["job_id"]) in cancelled_job_ids
            and date.fromisoformat(str(item["planning_date"])) > planning_date
        ]
        for event in events:
            payload = event.get("event_payload") or {}
            for key in ("lost_dates", "restored_dates"):
                affected_future_dates.extend(
                    value
                    for item in payload.get(key, [])
                    if (value := date.fromisoformat(str(item))) > planning_date
                )
        boundary_sensitive = all(
            item["event_type"]
            in {
                "JOB_CANCELLED",
                "ENGINEER_AVAILABILITY_LOST",
                "ENGINEER_AVAILABILITY_RESTORED",
            }
            for item in events
        )
        future_start = planning_date + timedelta(days=1)
        if boundary_sensitive and affected_future_dates:
            future_start = min(
                max(
                    planning_date + timedelta(days=1),
                    _seven_day_block_start(block_anchor, affected_date),
                )
                for affected_date in affected_future_dates
            )
        preserved_job_ids = {
            int(item["job_id"])
            for item in context["current_assignments"]
            if planning_date
            < date.fromisoformat(str(item["planning_date"]))
            < future_start
        }
        actor_user_id = next(
            (item.get("actor_user_id") for item in events if item.get("actor_user_id")),
            None,
        )
        batch_repository = SqlaPlanningBatchRepository(session)
        cascade_limit = int(
            context["source"]["config"].get("single_cascade_time_limit_sec", 900)
        )
        remaining_event_seconds = int(event_limit - (monotonic() - event_started))
        if remaining_event_seconds < 1:
            raise RuntimeError("EVENT_TIME_LIMIT")
        attempt = max(int(item.get("attempt_count") or 0) for item in events) + 1
        batch, reused = await batch_repository.create_or_reuse(
            project_id,
            planning_date,
            actor_user_id,
            "dynamic:"
            + ":".join(str(item["id"]) for item in events)
            + f":attempt-{attempt}",
            effective_start_override=future_start,
            excluded_job_ids=today_job_ids | preserved_job_ids,
            include_published_jobs=True,
            total_time_limit_override=min(remaining_event_seconds, cascade_limit),
        )
        batch_id = int(batch["id"])
        try:
            await repository.attach_batch(
                [int(item["id"]) for item in events], batch_id
            )
            if not reused or batch["status"] in {
                "CREATED",
                "PREPARING",
                "RUNNING",
                "STOP_REQUESTED",
            }:
                service = MultiDayPlanningService(
                    batch_repository,
                    PlanningInputNormalizer(geocoder),
                    OrToolsPlanningSolverFactory(matrix_factory),
                    PlanningValidator(),
                    PlanningBatchValidator(),
                )
                await service.execute(batch_id)
        except Exception as error:
            # The batch row is committed before it is attached to the dynamic
            # event. Never leave that row active when attachment or early batch
            # preparation fails, otherwise every subsequent event is blocked by
            # the one-active-batch-per-project constraint.
            await batch_repository.fail_batch(batch_id, "SYSTEM_ERROR", str(error))
            raise
        for data, result in context.get("today_solver_runs", []):
            await batch_repository.save_auxiliary_run(
                batch_id,
                planning_date,
                timezone_name,
                actor_user_id,
                data,
                result,
            )
        fresh_context = await repository.load_context(project_id, planning_date)
        if fresh_context["source_hash"] != context["source_hash"]:
            raise StaleDynamicSnapshot("Planning inputs changed during calculation")
        trigger_source = (
            next(iter(trigger_types)) if len(trigger_types) == 1 else "COALESCED"
        )
        version_id, input_hash = await repository.publish(
            project_id=project_id,
            event_ids=[int(item["id"]) for item in events],
            batch_id=batch_id,
            planning_date=planning_date,
            timezone_name=timezone_name,
            expected_fingerprint=context["fingerprint"],
            expected_source_hash=context["source_hash"],
            today_assignments=today_assignments,
            trigger_source=trigger_source,
            actor_user_id=actor_user_id,
        )
        await repository.complete(
            [int(item["id"]) for item in events], version_id, input_hash
        )

    async def _run(self, batch_id: int) -> None:
        async with self._advisory_lock(1_397_244_752, batch_id) as locked:
            if not locked:
                return
            async with self._sessionmaker() as session:
                repository = SqlaPlanningBatchRepository(session)
                geocoder = create_geocoder(session, self._config)
                matrix_factory = TravelMatrixProviderFactory(
                    StaticTravelMatrixProvider(),
                    ValhallaTravelMatrixProvider(session, self._config),
                )
                service = MultiDayPlanningService(
                    repository,
                    PlanningInputNormalizer(geocoder),
                    OrToolsPlanningSolverFactory(matrix_factory),
                    PlanningValidator(),
                    PlanningBatchValidator(),
                )
                try:
                    # Only the advisory-lock owner may recover an interrupted day.
                    await session.execute(
                        update(planning_runs)
                        .where(
                            planning_runs.c.planning_batch_id == batch_id,
                            planning_runs.c.status.in_(
                                ("CREATED", "PREPARING", "RUNNING")
                            ),
                        )
                        .values(
                            status="FAILED",
                            error_code="PROCESS_RESTARTED",
                            error_message="Interrupted before atomic day commit",
                        )
                    )
                    await session.execute(
                        update(planning_batch_days)
                        .where(
                            planning_batch_days.c.planning_batch_id == batch_id,
                            planning_batch_days.c.status == "RUNNING",
                        )
                        .values(status="PENDING", planning_run_id=None)
                    )
                    await session.commit()
                    await service.execute(batch_id)
                except Exception as error:
                    logger.exception(
                        "planning_batch_failed planning_batch_id=%s", batch_id
                    )
                    await repository.fail_batch(batch_id, "SYSTEM_ERROR", str(error))

    @asynccontextmanager
    async def _advisory_lock(self, namespace: int, key: int):
        """Hold a PostgreSQL session lock on one dedicated pooled connection."""

        async with self._engine.connect() as connection:
            locked = bool(
                await connection.scalar(
                    text("SELECT pg_try_advisory_lock(:namespace, :key)"),
                    {"namespace": namespace, "key": key},
                )
            )
            try:
                yield locked
            finally:
                if locked:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(:namespace, :key)"),
                        {"namespace": namespace, "key": key},
                    )
