from contextlib import asynccontextmanager

from dishka import AsyncContainer
from dishka.integrations.fastapi import setup_dishka
from fastapi import FastAPI

from auth.entrypoint.config import create_config
from auth.entrypoint.ioc.registry import get_providers
from auth.entrypoint.setup import (
    configure_app,
    configure_logging,
    create_app,
    create_async_ioc_container,
)
from auth.infrastructure.persistence_sqla.mappings.map import map_tables
from auth.presentation.http.base.root_router import root_router
from planning.application.interfaces.planning_batch_repository import (
    PlanningBatchExecutor,
)
from planning.infrastructure.adapters.job_import_csv import JobImportExecutor


def make_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Recover database-backed work interrupted by an application restart.
        executor = await async_ioc_container.get(PlanningBatchExecutor)
        import_executor = await async_ioc_container.get(JobImportExecutor)
        await executor.recover()
        await import_executor.recover()
        try:
            yield
        finally:
            await executor.shutdown()
            await import_executor.shutdown()

    config = create_config()
    app = create_app(lifespan=lifespan)
    map_tables()
    configure_app(app=app, root_router=root_router)

    async_ioc_container: AsyncContainer = create_async_ioc_container(
        providers=(*get_providers(),),
        config=config,
    )

    setup_dishka(container=async_ioc_container, app=app)
    configure_logging()
    return app
