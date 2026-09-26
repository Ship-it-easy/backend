"""Replay a saved multi-day batch against current code without publishing a plan.

The saved input snapshot is read from PostgreSQL. The ordinary Valhalla travel
cache can be warmed by this replay; planning batches, runs and plans are not
written. Use a failed demo batch to benchmark the same inputs after code edits.
"""

import argparse
import asyncio
import copy
import logging
import os
import time

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from auth.entrypoint.config import PostgresConfig
from planning.application.services.multi_day_planning import MultiDayPlanningService
from planning.application.services.planning_input_normalizer import (
    PlanningInputNormalizer,
)
from planning.application.validators.planning_batch import PlanningBatchValidator
from planning.application.validators.planning_result import PlanningValidator
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.geocoder_nominatim import NominatimGeocoder
from planning.infrastructure.adapters.planning_solver_ortools import (
    OrToolsPlanningSolverFactory,
)
from planning.infrastructure.adapters.travel_matrix_provider_factory import (
    TravelMatrixProviderFactory,
)
from planning.infrastructure.adapters.travel_matrix_provider_valhalla import (
    ValhallaTravelMatrixProvider,
)
from planning.infrastructure.persistence_sqla.mappings.tables import planning_batches


class ReplayRepository:
    def __init__(self, source: dict):
        self.source = source
        self.days = []
        self.started = {}
        self.outcome = None
        self.permanent = {}

    async def load_for_execution(self, _batch_id):
        return self.source

    async def should_stop(self, _batch_id):
        return False

    async def mark_batch_status(self, *_args):
        pass

    async def create_days(self, *_args):
        pass

    async def set_permanent_issues(self, _batch_id, issues):
        self.permanent.update(issues)

    async def skip_day(self, _batch_id, day, count):
        print(f"day={day} skipped=no_shift remaining={count}", flush=True)

    async def mark_day_running(self, _batch_id, day, job_ids):
        self.started[day] = time.monotonic()
        print(f"day={day} started candidates={len(job_ids)}", flush=True)

    async def create_daily_run(self, _batch_id, day, *_args):
        return len(self.days) + 1

    async def mark_daily_run_running(self, *_args):
        pass

    async def save_day_result(self, _batch_id, _run_id, data, result, _decisions):
        assigned = {job.job_id for route in result.routes for job in route.jobs}
        elapsed = time.monotonic() - self.started[data.planning_date]
        self.days.append((data, result, elapsed))
        correction = result.objective_metrics.get("traffic_model", {})
        print(
            f"day={data.planning_date} elapsed_s={elapsed:.1f} "
            f"assigned={len(assigned)} solver={result.solver_status} "
            f"corrections={correction.get('time_aware_correction_passes')} "
            f"objective={result.objective}",
            flush=True,
        )
        return assigned

    async def fail_day(self, _batch_id, day, _run_id, code, message):
        self.outcome = ("FAILED", code, message)
        print(f"day={day} failed={code} detail={message}", flush=True)

    async def fail_batch(self, _batch_id, code, message):
        self.outcome = ("FAILED", code, message)

    async def validate_terminal_state(self, *_args):
        return []

    async def finish_batch(self, _batch_id, status, reason, remaining, metrics):
        self.outcome = (status, reason, metrics, sorted(remaining))
        print(
            f"batch={status} reason={reason} assigned={metrics['assigned']} "
            f"remaining={len(remaining)} "
            f"duration_s={metrics['duration_ms'] / 1000:.1f}",
            flush=True,
        )
        if remaining:
            jobs = self.source["input_snapshot"]["jobs"]
            print(
                "remaining_jobs="
                + repr(
                    [
                        {
                            "id": job["id"],
                            "required_transport": job.get("required_transport"),
                            "service_duration_min": job.get("service_duration_min"),
                            "time_window_start": job.get("time_window_start"),
                            "time_window_end": job.get("time_window_end"),
                        }
                        for job in jobs
                        if job["id"] in remaining
                    ]
                ),
                flush=True,
            )


async def replay(batch_id: int, seconds: int, transport_type: str | None = None):
    load_dotenv(".env.docker", override=True)
    os.environ["POSTGRES_HOST"] = "localhost"
    os.environ["VALHALLA_URL"] = "http://localhost:8002"
    engine = create_async_engine(PostgresConfig.from_env().uri)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    started = time.monotonic()
    try:
        async with session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(planning_batches).where(
                            planning_batches.c.id == batch_id
                        )
                    )
                )
                .mappings()
                .one()
            )
            source = copy.deepcopy(dict(row))
            source["status"] = "CREATED"
            source["started_at"] = None
            source["execution_job_states"] = []
            source["execution_day_states"] = []
            source["input_snapshot"]["config"]["batch_total_time_limit_sec"] = seconds
            if transport_type is not None:
                for engineer in source["input_snapshot"]["engineers"]:
                    engineer["transport_type"] = transport_type
            repository = ReplayRepository(source)
            config = PlanningServiceConfig.from_env()
            geocoder = NominatimGeocoder(session, config)
            matrix_provider = ValhallaTravelMatrixProvider(session, config)
            solver_factory = OrToolsPlanningSolverFactory(
                TravelMatrixProviderFactory(None, matrix_provider),
                baseline_comparison_enabled=False,
            )
            service = MultiDayPlanningService(
                repository,
                PlanningInputNormalizer(geocoder),
                solver_factory,
                PlanningValidator(),
                PlanningBatchValidator(),
            )
            print(
                f"batch_id={batch_id} jobs={len(source['input_snapshot']['jobs'])} "
                f"engineers={len(source['input_snapshot']['engineers'])} "
                f"budget_s={seconds} transport={transport_type or 'snapshot'}",
                flush=True,
            )
            await service.execute(batch_id)
            print(f"wall_s={time.monotonic() - started:.1f}", flush=True)
            return repository.outcome
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id", type=int, required=True)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--transport-type", choices=("CAR", "BICYCLE", "NONE"))
    arguments = parser.parse_args()
    outcome = asyncio.run(
        replay(arguments.batch_id, arguments.seconds, arguments.transport_type)
    )
    if outcome is None or outcome[0] != "SUCCESS":
        raise SystemExit(1)
