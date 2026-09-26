import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from auth.domain.user_role import UserRoleEnum
from auth.infrastructure.persistence_sqla.mappings.user import users_table
from auth.infrastructure.persistence_sqla.orm_registry import metadata_obj
from planning.application.errors import ConflictError, InvalidPlanningRequest
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters import job_import_csv as job_import_module
from planning.infrastructure.adapters.address_search_nominatim import (
    NominatimAddressSearchProvider,
)
from planning.infrastructure.adapters.job_import_csv import (
    JobImportExecutor,
    JobImportService,
)
from planning.infrastructure.persistence_sqla.mappings.tables import (
    dispatcher_projects,
    job_import_batches,
    job_import_rows,
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
                    project_id=None,
                )
            )
            await session.execute(
                insert(dispatcher_projects).values(
                    user_id=actor_id, project_id=project_id
                )
            )
            await session.commit()

        async def address_search(self, query, *, require_house=True):
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
        dispatch = SimpleNamespace(
            schedule=lambda *_args: None,
            address_provider_version="nominatim-v1",
        )
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
        config = PlanningServiceConfig(
            valhalla_url="http://test",
            nominatim_url="http://test",
            nominatim_viewbox="",
            geoservice_timeout_sec=1,
            matrix_block_size=40,
        )
        worker = JobImportExecutor(factory, config)
        second_worker = JobImportExecutor(factory, config)
        async with worker._validation_lock(project_id) as first_lock:
            async with second_worker._validation_lock(project_id) as second_lock:
                assert first_lock is True
                assert second_lock is False
            queued_validation = asyncio.create_task(
                second_worker._validate(batch["id"], project_id)
            )
            await asyncio.sleep(0.3)
            assert queued_validation.done() is False
        await asyncio.wait_for(queued_validation, timeout=5)
        async with factory() as session:
            service = JobImportService(session, access, dispatch, planner)
            ready = await service.get(project_id, batch["id"])
            assert ready["status"] == "READY_TO_APPLY"
            idempotency_key = str(uuid.uuid4())
            applied = await service.apply(
                project_id, batch["id"], idempotency_key=idempotency_key
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
            assert job["external_id"] is None
            batch_created_at = await session.scalar(
                select(job_import_batches.c.created_at).where(
                    job_import_batches.c.id == batch["id"]
                )
            )
            assert job["received_at"] == batch_created_at
            assert job["ingest_sequence"] == 1
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(planning_events)
                    .where(planning_events.c.event_type == "JOBS_IMPORTED")
                )
                == 1
            )
            repeated = await service.apply(
                project_id, batch["id"], idempotency_key=idempotency_key
            )
            assert repeated["created_count"] == 1
            with pytest.raises(ConflictError) as already_applied:
                await service.apply(
                    project_id, batch["id"], idempotency_key=str(uuid.uuid4())
                )
            assert already_applied.value.code == "IMPORT_ALREADY_APPLIED"

        async with factory() as session:
            service = JobImportService(session, access, dispatch, planner)
            with pytest.raises(ConflictError) as duplicate_file:
                await service.upload(project_id, "same.csv", source)
            assert duplicate_file.value.code == "FILE_ALREADY_APPLIED"
            assert duplicate_file.value.details == {"batch_id": batch["id"]}

        warning_source = (
            "Тип заявки ВК;Начало;Окончание;Адрес;Комментарий\n"
            "Монтаж;17.09.2026 9:00;17.09.2026 10:00;Пермь, Ленина, 5;повтор\n"
        ).encode("utf-8")
        async with factory() as session:
            service = JobImportService(session, access, dispatch, planner)
            warning_batch = await service.upload(
                project_id, "warning.csv", warning_source
            )
        await worker._validate_inner(warning_batch["id"])
        async with factory() as session:
            service = JobImportService(session, access, dispatch, planner)
            warning_ready = await service.get(project_id, warning_batch["id"])
            assert warning_ready["status"] == "READY_TO_APPLY"
            assert warning_ready["warning_count"] == 1
            with pytest.raises(ConflictError) as warning_error:
                await service.apply(
                    project_id,
                    warning_batch["id"],
                    idempotency_key=str(uuid.uuid4()),
                )
            assert warning_error.value.code == "WARNINGS_NOT_ACKNOWLEDGED"
            warning_applied = await service.apply(
                project_id,
                warning_batch["id"],
                acknowledge_warnings=True,
                idempotency_key=str(uuid.uuid4()),
            )
            assert warning_applied["created_count"] == 1
            assert warning_applied["warnings_acknowledged_by"] == actor_id

        duplicate_rows_source = (
            "Тип заявки ВК;Начало;Окончание;Адрес\n"
            "Монтаж;18.09.2026 9:00;18.09.2026 10:00;Пермь, Ленина, 5\n"
            "Монтаж;18.09.2026 9:00;18.09.2026 10:00;Пермь, Ленина, 5\n"
        ).encode("utf-8")
        async with factory() as session:
            service = JobImportService(session, access, dispatch, planner)
            duplicate_batch = await service.upload(
                project_id, "duplicate.csv", duplicate_rows_source
            )
        await worker._validate_inner(duplicate_batch["id"])
        async with factory() as session:
            service = JobImportService(session, access, dispatch, planner)
            duplicate_result = await service.get(project_id, duplicate_batch["id"])
            duplicate_issues = await service.issues(
                project_id, duplicate_batch["id"], query="DUPLICATE_IN_FILE"
            )
            assert duplicate_result["status"] == "HAS_ERRORS"
            assert duplicate_issues["total"] == 2
            with pytest.raises(InvalidPlanningRequest) as import_has_errors:
                await service.apply(
                    project_id,
                    duplicate_batch["id"],
                    idempotency_key=str(uuid.uuid4()),
                )
            assert import_has_errors.value.code == "IMPORT_HAS_ERRORS"

        stale_source = (
            "Тип заявки ВК;Начало;Окончание;Адрес\n"
            "Монтаж;19.09.2026 11:00;19.09.2026 12:00;Пермь, Ленина, 5\n"
        ).encode("utf-8")
        async with factory() as session:
            service = JobImportService(session, access, dispatch, planner)
            stale_batch = await service.upload(project_id, "stale.csv", stale_source)
        await worker._validate_inner(stale_batch["id"])
        async with factory() as session:
            await session.execute(
                update(work_types)
                .where(work_types.c.id == work_type_id)
                .values(name="Монтаж новый")
            )
            await session.commit()
        async with factory() as session:
            service = JobImportService(session, access, dispatch, planner)
            with pytest.raises(ConflictError) as stale_error:
                await service.apply(
                    project_id,
                    stale_batch["id"],
                    idempotency_key=str(uuid.uuid4()),
                )
            assert stale_error.value.code == "STALE_VALIDATION"
            assert (
                await session.scalar(
                    select(job_import_batches.c.status).where(
                        job_import_batches.c.id == stale_batch["id"]
                    )
                )
                == "STALE_VALIDATION"
            )

        async with factory() as session:
            await session.execute(
                update(work_types)
                .where(work_types.c.id == work_type_id)
                .values(name="Монтаж")
            )
            await session.commit()

        rollback_source = (
            "Тип заявки ВК;Начало;Окончание;Адрес\n"
            "Монтаж;20.09.2026 9:00;20.09.2026 10:00;Пермь, Ленина, 5\n"
            "Монтаж;20.09.2026 10:00;20.09.2026 11:00;Пермь, Ленина, 5\n"
        ).encode("utf-8")
        async with factory() as session:
            service = JobImportService(session, access, dispatch, planner)
            rollback_batch = await service.upload(
                project_id, "rollback.csv", rollback_source
            )
        await worker._validate_inner(rollback_batch["id"])
        fixed_uuid = SimpleNamespace(hex="A" * 32)
        monkeypatch.setattr(
            job_import_module,
            "uuid",
            SimpleNamespace(uuid4=lambda: fixed_uuid),
        )
        async with factory() as session:
            service = JobImportService(session, access, dispatch, planner)
            with pytest.raises(IntegrityError):
                await service.apply(
                    project_id,
                    rollback_batch["id"],
                    idempotency_key="00000000-0000-4000-8000-000000000001",
                )
        async with factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(jobs)
                    .where(jobs.c.import_batch_id == rollback_batch["id"])
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(job_import_batches.c.status).where(
                        job_import_batches.c.id == rollback_batch["id"]
                    )
                )
                == "READY_TO_APPLY"
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(job_import_rows)
                    .where(
                        job_import_rows.c.batch_id == rollback_batch["id"],
                        job_import_rows.c.created_job_id.is_not(None),
                    )
                )
                == 0
            )
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


async def _actor(actor_id):
    return SimpleNamespace(id=actor_id)
