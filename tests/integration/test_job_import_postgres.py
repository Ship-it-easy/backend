import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from auth.domain.user_role import UserRoleEnum
from auth.infrastructure.persistence_sqla.mappings.user import users_table
from auth.infrastructure.persistence_sqla.orm_registry import metadata_obj
from planning.infrastructure.adapters.address_search_nominatim import (
    NominatimAddressSearchProvider,
)
from planning.infrastructure.adapters.job_import_csv import (
    JobImportExecutor,
    JobImportService,
)
from planning.infrastructure.persistence_sqla.mappings.tables import (
    jobs,
    planning_events,
    projects,
    work_types,
)

DSN = os.getenv("TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_POSTGRES_DSN is required")


@pytest.mark.asyncio
async def test_csv_validation_and_atomic_apply(monkeypatch):
    schema = f"job_import_test_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(DSN)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        DSN, execution_options={"schema_translate_map": {None: schema}}
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata_obj.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        actor_id = uuid.uuid4()
        async with factory() as session:
            project_id = await session.scalar(
                insert(projects)
                .values(
                    name="CSV test",
                    internal_code=f"P-{uuid.uuid4().hex}",
                    planning_timezone="Asia/Yekaterinburg",
                    status="ACTIVE",
                )
                .returning(projects.c.id)
            )
            work_type_id = await session.scalar(
                insert(work_types)
                .values(
                    project_id=project_id,
                    code="INSTALL",
                    name="Монтаж",
                    active=True,
                    default_service_duration_min=45,
                )
                .returning(work_types.c.id)
            )
            await session.execute(
                insert(users_table).values(
                    id=actor_id,
                    username=f"csv-{uuid.uuid4().hex}",
                    password_hash="hash",
                    is_active=True,
                    role=UserRoleEnum.DISPATCHER,
                    is_verified=True,
                    project_id=project_id,
                )
            )
            await session.commit()

        async def address_search(self, query):
            return [
                {
                    "display_name": "Россия, Пермь, улица Ленина, 5",
                    "address": {
                        "country_code": "ru",
                        "city": "Пермь",
                        "road": "улица Ленина",
                        "house_number": "5",
                    },
                    "address_key": "house-5",
                    "latitude": 58.0,
                    "longitude": 56.0,
                }
            ]

        monkeypatch.setattr(NominatimAddressSearchProvider, "search", address_search)
        access = SimpleNamespace(project=lambda *_args, **_kwargs: _actor(actor_id))
        scheduled = []
        dispatch = SimpleNamespace(schedule=lambda *_args: None)
        planner = SimpleNamespace(
            schedule_project=lambda project: scheduled.append(project)
        )
        source = (
            "Тип заявки ВК;Начало;Окончание;Адрес\n"
            "Монтаж;17.09.2026 9:00;17.09.2026 10:00;Пермь, Ленина, 5\n"
        ).encode("utf-8")
        async with factory() as session:
            service = JobImportService(session, access, dispatch, planner)
            batch = await service.upload(project_id, "jobs.csv", source)
        worker = JobImportExecutor(factory, None)
        await worker._validate_inner(batch["id"])
        async with factory() as session:
            service = JobImportService(session, access, dispatch, planner)
            ready = await service.get(project_id, batch["id"])
            assert ready["status"] == "READY_TO_APPLY"
            applied = await service.apply(
                project_id, batch["id"], idempotency_key=str(uuid.uuid4())
            )
            assert applied["created_count"] == 1
            assert applied["status"] == "APPLIED"
            assert scheduled == [project_id]
            job = (
                (
                    await session.execute(
                        select(jobs).where(jobs.c.import_batch_id == batch["id"])
                    )
                )
                .mappings()
                .one()
            )
            assert job["work_type_id"] == work_type_id
            assert job["service_duration_min"] == 45
            assert job["status"] == "NEW"
            assert job["priority_type"] == "NORMAL"
            assert job["external_id"] is None
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(planning_events)
                    .where(planning_events.c.event_type == "JOBS_IMPORTED")
                )
                == 1
            )
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


async def _actor(actor_id):
    return SimpleNamespace(id=actor_id)
