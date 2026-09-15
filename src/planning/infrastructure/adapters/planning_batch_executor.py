import asyncio
import logging

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from planning.application.services.multi_day_planning import MultiDayPlanningService
from planning.application.services.planning_input_normalizer import (
    PlanningInputNormalizer,
)
from planning.application.validators.planning_batch import PlanningBatchValidator
from planning.application.validators.planning_result import PlanningValidator
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.geocoder_nominatim import NominatimGeocoder
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
    planning_runs,
)

logger = logging.getLogger(__name__)


class InProcessPlanningBatchExecutor:
    """Runs a durable database-backed batch outside the request-scoped session.

    The database is the source of truth for status and progress. Keeping only task
    handles in memory prevents duplicate scheduling inside one worker; a CREATED or
    RUNNING row can safely be scheduled again by operational recovery code.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        config: PlanningServiceConfig,
    ):
        self._sessionmaker = sessionmaker
        self._config = config
        self._tasks: dict[int, asyncio.Task] = {}

    def schedule(self, batch_id: int) -> None:
        current = self._tasks.get(batch_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._run(batch_id), name=f"planning-batch-{batch_id}"
        )
        self._tasks[batch_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(batch_id, None))

    async def recover(self) -> None:
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
                            )
                        )
                    )
                ).all()
            ]
        for batch_id in batch_ids:
            self.schedule(batch_id)

    async def _run(self, batch_id: int) -> None:
        async with self._sessionmaker() as session:
            locked = await session.scalar(
                text("SELECT pg_try_advisory_lock(:namespace, :batch_id)"),
                {"namespace": 1_397_244_752, "batch_id": batch_id},
            )
            if not locked:
                return
            repository = SqlaPlanningBatchRepository(session)
            geocoder = NominatimGeocoder(session, self._config)
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
                        error_code="WORKER_RESTARTED",
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
                logger.exception("planning_batch_failed planning_batch_id=%s", batch_id)
                await repository.fail_batch(batch_id, "SYSTEM_ERROR", str(error))
            finally:
                await session.execute(
                    text("SELECT pg_advisory_unlock(:namespace, :batch_id)"),
                    {"namespace": 1_397_244_752, "batch_id": batch_id},
                )
