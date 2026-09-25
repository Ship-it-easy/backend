import asyncio
import os
import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from auth.domain.user_role import UserRoleEnum
from auth.infrastructure.persistence_sqla.mappings.user import users_table
from auth.infrastructure.persistence_sqla.orm_registry import metadata_obj
from planning.application.errors import ConflictError
from planning.application.interactors.admin.users import BlockUserInteractor
from planning.application.interactors.project.planning import (
    PublishPlanningRunInteractor,
)
from planning.infrastructure.adapters.admin_management_repositories_sqla import (
    SqlaAdminUserRepository,
)
from planning.infrastructure.adapters.dynamic_planning_repository_sqla import (
    SqlaDynamicPlanningRepository,
)
from planning.infrastructure.adapters.planning_management_repository_sqla import (
    SqlaPlanningManagementRepository,
)
from planning.infrastructure.adapters.project_access_repository_sqla import (
    SqlaProjectAccessRepository,
)
from planning.infrastructure.adapters.project_management_repositories_sqla import (
    SqlaEngineerAccountRepository,
)
from planning.infrastructure.adapters.transaction_manager_sqla import (
    SqlAlchemyTransactionManager,
)
from planning.infrastructure.persistence_sqla.mappings.tables import (
    assignments,
    daily_plans,
    dispatcher_projects,
    engineer_schedules,
    engineers,
    equipment_types,
    jobs,
    plan_versions,
    planning_batch_days,
    planning_batches,
    planning_cancelled_job_snapshots,
    planning_day_results,
    planning_events,
    planning_route_jobs,
    planning_routes,
    planning_runs,
    planning_unassigned_jobs,
    project_plan_assignments,
    project_plan_versions,
    projects,
    work_type_required_equipment,
    work_types,
)

TEST_DSN = os.getenv("TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="TEST_POSTGRES_DSN is required for PostgreSQL integration tests",
)


@pytest.fixture
async def pg_engine() -> AsyncEngine:
    assert TEST_DSN is not None
    schema = f"planning_test_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(TEST_DSN)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        TEST_DSN,
        execution_options={"schema_translate_map": {None: schema}},
    )
    async with engine.begin() as connection:
        await connection.run_sync(metadata_obj.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


async def seed_project(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            insert(projects)
            .values(
                name=f"Project {uuid.uuid4().hex}",
                internal_code=f"PRJ-{uuid.uuid4().hex}",
                planning_timezone="UTC",
                status="ACTIVE",
            )
            .returning(projects.c.id)
        )
    )


def owner_values(user_id: uuid.UUID, login: str) -> dict:
    return {
        "id": user_id,
        "username": login,
        "password_hash": "hash",
        "is_active": True,
        "role": UserRoleEnum.OWNER,
        "is_verified": True,
    }


async def test_concurrent_owner_block_keeps_one_active(pg_engine: AsyncEngine) -> None:
    owner_ids = (uuid.uuid4(), uuid.uuid4())
    async with pg_engine.begin() as connection:
        await connection.execute(
            insert(users_table),
            [
                owner_values(owner_ids[0], f"owner-{uuid.uuid4().hex}"),
                owner_values(owner_ids[1], f"owner-{uuid.uuid4().hex}"),
            ],
        )

    gate = asyncio.Event()
    ready = 0
    ready_lock = asyncio.Lock()

    async def block(user_id: uuid.UUID):
        nonlocal ready
        async with pg_engine.connect() as connection:
            session = AsyncSession(bind=connection, expire_on_commit=False)
            access = AsyncMock()
            access.owner.return_value = MagicMock(id=uuid.uuid4())
            interactor = BlockUserInteractor(
                access,
                SqlaAdminUserRepository(session),
                MagicMock(),
                SqlAlchemyTransactionManager(session),
            )
            async with ready_lock:
                ready += 1
                if ready == 2:
                    gate.set()
            await gate.wait()
            try:
                return await interactor(user_id)
            except ConflictError as error:
                return error
            finally:
                await session.close()

    results = await asyncio.gather(*(block(user_id) for user_id in owner_ids))
    assert sum(isinstance(value, ConflictError) for value in results) == 1
    assert (
        next(value for value in results if isinstance(value, ConflictError)).code
        == "LAST_ACTIVE_OWNER"
    )
    async with pg_engine.connect() as connection:
        active = await connection.scalar(
            select(func.count())
            .select_from(users_table)
            .where(
                users_table.c.role.in_([UserRoleEnum.OWNER, UserRoleEnum.ADMIN]),
                users_table.c.is_active.is_(True),
            )
        )
    assert active == 1


async def test_concurrent_admin_login_creation_maps_conflict(
    pg_engine: AsyncEngine,
) -> None:
    login = f"same-{uuid.uuid4().hex}"

    async def create():
        async with AsyncSession(pg_engine, expire_on_commit=False) as session:
            try:
                result = await SqlaAdminUserRepository(session).create_user(
                    login, "hash", UserRoleEnum.OWNER
                )
                await session.commit()
                return result
            except ConflictError as error:
                return error

    results = await asyncio.gather(create(), create())
    assert sum(isinstance(value, ConflictError) for value in results) == 1
    assert (
        next(value for value in results if isinstance(value, ConflictError)).code
        == "LOGIN_EXISTS"
    )


async def test_dispatcher_project_memberships_are_independent_and_filter_blocked(
    pg_engine: AsyncEngine,
) -> None:
    dispatcher_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    async with AsyncSession(pg_engine, expire_on_commit=False) as session:
        project_ids = [await seed_project(session), await seed_project(session)]
        blocked_id = await seed_project(session)
        await session.execute(
            update(projects).where(projects.c.id == blocked_id).values(status="BLOCKED")
        )
        values = owner_values(dispatcher_id, f"dispatcher-{uuid.uuid4().hex}")
        values.update(role=UserRoleEnum.DISPATCHER, project_id=None)
        await session.execute(insert(users_table).values(**values))
        await session.execute(
            insert(users_table).values(
                **owner_values(owner_id, f"owner-{uuid.uuid4().hex}")
            )
        )
        await session.execute(
            insert(dispatcher_projects),
            [
                {"user_id": dispatcher_id, "project_id": project_id}
                for project_id in [*project_ids, blocked_id]
            ],
        )
        await session.commit()

        access = SqlaProjectAccessRepository(session)
        assert await access.dispatcher_has_project(dispatcher_id, project_ids[0])
        active = await access.list_dispatcher_projects(dispatcher_id, active_only=True)
        assert {row["id"] for row in active} == set(project_ids)

        dispatchers = await SqlaAdminUserRepository(session).list_dispatchers()
        assert dispatchers == [
            {
                "id": str(dispatcher_id),
                "login": values["username"],
                "role": "dispatcher",
                "project_id": None,
                "engineer_id": None,
                "status": "ACTIVE",
                "project_ids": sorted([*project_ids, blocked_id]),
            }
        ]

        repository = SqlaAdminUserRepository(session)
        single = await repository.replace_dispatcher_projects(
            dispatcher_id, [project_ids[0]], owner_id
        )
        await session.commit()
        assert single["project_id"] == project_ids[0]
        assert (
            await session.scalar(
                select(users_table.c.project_id).where(
                    users_table.c.id == dispatcher_id
                )
            )
            == project_ids[0]
        )

        multiple = await repository.replace_dispatcher_projects(
            dispatcher_id, project_ids, owner_id
        )
        await session.commit()
        assert multiple["project_id"] is None
        assert (
            await session.scalar(
                select(users_table.c.project_id).where(
                    users_table.c.id == dispatcher_id
                )
            )
            is None
        )


async def test_concurrent_engineer_login_creation_maps_conflict(
    pg_engine: AsyncEngine,
) -> None:
    async with AsyncSession(pg_engine, expire_on_commit=False) as session:
        project_id = await seed_project(session)
        engineer_ids = []
        for _ in range(2):
            engineer_ids.append(
                int(
                    await session.scalar(
                        insert(engineers)
                        .values(
                            project_id=project_id,
                            name="Engineer",
                            internal_code=f"ENG-{uuid.uuid4().hex}",
                            active=True,
                            transport_type="NONE",
                            start_address="Office",
                        )
                        .returning(engineers.c.id)
                    )
                )
            )
        await session.commit()
    login = f"engineer-{uuid.uuid4().hex}"

    async def create(engineer_id: int):
        async with AsyncSession(pg_engine, expire_on_commit=False) as session:
            try:
                result = await SqlaEngineerAccountRepository(session).create_account(
                    project_id, engineer_id, login, "hash"
                )
                await session.commit()
                return result
            except ConflictError as error:
                return error

    results = await asyncio.gather(*(create(value) for value in engineer_ids))
    assert sum(isinstance(value, ConflictError) for value in results) == 1
    assert (
        next(value for value in results if isinstance(value, ConflictError)).code
        == "LOGIN_EXISTS"
    )


async def seed_publication(session: AsyncSession) -> tuple[int, int, uuid.UUID]:
    project_id = await seed_project(session)
    user_id = uuid.uuid4()
    user = owner_values(user_id, f"dispatcher-{uuid.uuid4().hex}")
    user.update(role=UserRoleEnum.DISPATCHER, project_id=None)
    await session.execute(insert(users_table).values(**user))
    await session.execute(
        insert(dispatcher_projects).values(user_id=user_id, project_id=project_id)
    )
    work_type_id = await session.scalar(
        insert(work_types)
        .values(
            project_id=project_id,
            code=f"WT-{uuid.uuid4().hex}",
            name="Work",
            active=True,
            default_service_duration_min=30,
        )
        .returning(work_types.c.id)
    )
    engineer_id = await session.scalar(
        insert(engineers)
        .values(
            project_id=project_id,
            name="Engineer",
            internal_code=f"ENG-{uuid.uuid4().hex}",
            active=True,
            transport_type="NONE",
            start_address="Office",
        )
        .returning(engineers.c.id)
    )
    job_id = await session.scalar(
        insert(jobs)
        .values(
            project_id=project_id,
            internal_code=f"JOB-{uuid.uuid4().hex}",
            status="NEW",
            address="Address",
            sla_date=date(2026, 1, 1),
            work_type_id=work_type_id,
            service_duration_min=30,
        )
        .returning(jobs.c.id)
    )
    run_id = await session.scalar(
        insert(planning_runs)
        .values(
            project_id=project_id,
            planning_date=date(2026, 1, 1),
            timezone="UTC",
            status="SUCCESS",
            input_snapshot={"jobs": [{"id": int(job_id)}]},
            validation_errors=[],
        )
        .returning(planning_runs.c.id)
    )
    route_id = await session.scalar(
        insert(planning_routes)
        .values(
            planning_run_id=run_id,
            engineer_id=engineer_id,
            planned_start=datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
            planned_finish=datetime(2026, 1, 1, 9, tzinfo=timezone.utc),
            total_travel_min=0,
            total_service_min=30,
            total_waiting_min=0,
        )
        .returning(planning_routes.c.id)
    )
    await session.execute(
        insert(planning_route_jobs).values(
            planning_route_id=route_id,
            planning_run_id=run_id,
            job_id=job_id,
            sequence=1,
            planned_arrival=datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
            planned_start=datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
            planned_finish=datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc),
            travel_from_previous_min=0,
            waiting_before_job_min=0,
            drop_penalty_snapshot=1,
        )
    )
    await session.commit()
    return project_id, int(run_id), user_id


async def publish_with(
    session: AsyncSession,
    repository: SqlaPlanningManagementRepository,
    project_id: int,
    run_id: int,
    user_id: uuid.UUID,
) -> dict:
    access = AsyncMock()
    access.dispatcher.return_value = (MagicMock(id=user_id), project_id)
    return await PublishPlanningRunInteractor(
        access, repository, SqlAlchemyTransactionManager(session)
    )(run_id, True)


async def test_publication_is_atomic(pg_engine: AsyncEngine) -> None:
    async with AsyncSession(pg_engine, expire_on_commit=False) as session:
        project_id, run_id, user_id = await seed_publication(session)
        result = await publish_with(
            session,
            SqlaPlanningManagementRepository(session),
            project_id,
            run_id,
            user_id,
        )
    assert result["assignments_count"] == 1
    async with pg_engine.connect() as connection:
        assert (
            await connection.scalar(select(func.count()).select_from(daily_plans)) == 1
        )
        assert (
            await connection.scalar(select(func.count()).select_from(plan_versions))
            == 1
        )
        assert (
            await connection.scalar(select(func.count()).select_from(assignments)) == 1
        )


async def test_publication_rolls_back_midway(pg_engine: AsyncEngine) -> None:
    class FailingRepository(SqlaPlanningManagementRepository):
        async def save_publication(self, command):
            await super().save_publication(command)
            raise RuntimeError("failure after persistence writes")

    async with AsyncSession(pg_engine, expire_on_commit=False) as session:
        project_id, run_id, user_id = await seed_publication(session)
        with pytest.raises(RuntimeError):
            await publish_with(
                session,
                FailingRepository(session),
                project_id,
                run_id,
                user_id,
            )
    async with pg_engine.connect() as connection:
        assert (
            await connection.scalar(select(func.count()).select_from(daily_plans)) == 0
        )
        assert (
            await connection.scalar(select(func.count()).select_from(plan_versions))
            == 0
        )
        assert (
            await connection.scalar(select(func.count()).select_from(assignments)) == 0
        )


async def test_planning_board_reads_one_outcome_per_day_input(
    pg_engine: AsyncEngine,
) -> None:
    planning_date = date(2026, 9, 19)
    async with AsyncSession(pg_engine, expire_on_commit=False) as session:
        project_id = await seed_project(session)
        work_type_id = await session.scalar(
            insert(work_types)
            .values(
                project_id=project_id,
                code="REPAIR",
                name="Диагностика линии",
                default_service_duration_min=45,
            )
            .returning(work_types.c.id)
        )
        engineer_id = await session.scalar(
            insert(engineers)
            .values(
                project_id=project_id,
                internal_code="ENG-BOARD",
                name="Иван Петров",
                transport_type="CAR",
                start_address="База",
                start_latitude=58.010455,
                start_longitude=56.229443,
            )
            .returning(engineers.c.id)
        )
        await session.execute(
            insert(engineer_schedules).values(
                engineer_id=engineer_id,
                work_date=planning_date,
                shift_start="08:00:00",
                shift_end="18:00:00",
            )
        )
        job_ids = []
        for suffix, address in (
            ("A", "Ленина, 1"),
            ("B", "Ленина, 2"),
        ):
            job_ids.append(
                int(
                    await session.scalar(
                        insert(jobs)
                        .values(
                            project_id=project_id,
                            internal_code=f"BOARD-{suffix}",
                            status="NEW",
                            address=address,
                            latitude=58.010000 + len(job_ids) / 1000,
                            longitude=56.230000 + len(job_ids) / 1000,
                            sla_date=planning_date,
                            work_type_id=work_type_id,
                            service_duration_min=45,
                        )
                        .returning(jobs.c.id)
                    )
                )
            )
        snapshot_jobs = [
            {
                "id": job_id,
                "address": f"Ленина, {index}",
                "latitude": str(58.010000 + (index - 1) / 1000),
                "longitude": str(56.230000 + (index - 1) / 1000),
                "sla_date": planning_date.isoformat(),
                "work_type_id": int(work_type_id),
                "work_type_name": "Диагностика линии",
                "service_duration_min": 45,
                "priority": "LOW",
            }
            for index, job_id in enumerate(job_ids, start=1)
        ]
        batch_id = await session.scalar(
            insert(planning_batches)
            .values(
                project_id=project_id,
                requested_start_date=planning_date,
                effective_start_date=planning_date,
                initial_horizon_end=planning_date,
                maximum_horizon_end=planning_date,
                status="PARTIAL",
                completion_reason="HORIZON_LIMIT",
                idempotency_key="board-test",
                input_hash="snapshot",
                configuration_version="1",
                input_snapshot={
                    "jobs": snapshot_jobs,
                    "engineers": [
                        {
                            "id": int(engineer_id),
                            "name": "Иван Петров",
                            "transport_type": "CAR",
                            "start_address": "База",
                            "start_latitude": "58.010455",
                            "start_longitude": "56.229443",
                        }
                    ],
                    "schedules": [
                        {
                            "engineer_id": int(engineer_id),
                            "work_date": planning_date.isoformat(),
                            "shift_start": "08:00:00",
                            "shift_end": "18:00:00",
                        }
                    ],
                },
            )
            .returning(planning_batches.c.id)
        )
        run_id = await session.scalar(
            insert(planning_runs)
            .values(
                project_id=project_id,
                planning_batch_id=batch_id,
                planning_date=planning_date,
                timezone="UTC",
                status="SUCCESS",
                solver_status="OPTIMAL",
                input_snapshot={
                    "jobs": [
                        {
                            "id": job_id,
                            "sla_date": planning_date.isoformat(),
                            "priority": "LOW",
                            "required_qualifications": [],
                        }
                        for job_id in job_ids
                    ],
                    "engineers": [
                        {
                            "id": int(engineer_id),
                            "transport_type": "CAR",
                            "qualifications": [],
                        }
                    ],
                },
                input_jobs_count=2,
                assigned_jobs_count=1,
                unassigned_jobs_count=1,
                validation_errors=[],
            )
            .returning(planning_runs.c.id)
        )
        await session.execute(
            insert(planning_batch_days).values(
                planning_batch_id=batch_id,
                planning_date=planning_date,
                block_number=1,
                status="SUCCESS",
                planning_run_id=run_id,
                input_jobs_count=2,
                assigned_count=1,
                unassigned_count=1,
            )
        )
        previous_version_id = await session.scalar(
            insert(project_plan_versions)
            .values(
                project_id=project_id,
                version_number=1,
                planning_batch_id=batch_id,
                input_hash="published",
                trigger_source="MANUAL",
                is_current=False,
                unassigned_jobs=[
                    {
                        "job_id": job_ids[1],
                        "primary_reason_code": "NOT_ASSIGNED_WITHIN_HORIZON",
                    }
                ],
            )
            .returning(project_plan_versions.c.id)
        )
        next_batch_id = await session.scalar(
            insert(planning_batches)
            .values(
                project_id=project_id,
                requested_start_date=planning_date,
                effective_start_date=planning_date,
                initial_horizon_end=planning_date,
                maximum_horizon_end=planning_date,
                status="SUCCESS",
                completion_reason="ALL_ELIGIBLE_ASSIGNED",
                idempotency_key="board-test-next",
                input_hash="snapshot-next",
                configuration_version="1",
                input_snapshot={"jobs": [], "engineers": [], "schedules": []},
            )
            .returning(planning_batches.c.id)
        )
        version_id = await session.scalar(
            insert(project_plan_versions)
            .values(
                project_id=project_id,
                version_number=2,
                planning_batch_id=next_batch_id,
                input_hash="published-next",
                trigger_source="JOB_CREATED",
                is_current=True,
                unassigned_jobs=[
                    {
                        "job_id": job_ids[1],
                        "primary_reason_code": "NOT_ASSIGNED_WITHIN_HORIZON",
                    }
                ],
            )
            .returning(project_plan_versions.c.id)
        )
        await session.execute(
            insert(planning_events).values(
                project_id=project_id,
                event_type="JOB_CREATED",
                job_ids=[job_ids[0]],
                initiator="SYSTEM",
                state="PUBLISHED",
                published_plan_version_id=version_id,
            )
        )
        day_result_id = await session.scalar(
            insert(planning_day_results)
            .values(
                plan_version_id=version_id,
                project_id=project_id,
                planning_date=planning_date,
                planning_run_id=run_id,
                input_jobs_count=2,
                assigned_count=1,
                unassigned_count=1,
                solver_status="OPTIMAL",
            )
            .returning(planning_day_results.c.id)
        )
        await session.execute(
            insert(project_plan_assignments).values(
                plan_version_id=version_id,
                project_id=project_id,
                job_id=job_ids[0],
                planning_date=planning_date,
                engineer_id=engineer_id,
                sequence=1,
                planned_arrival=datetime(2026, 9, 19, 8, tzinfo=timezone.utc),
                planned_start=datetime(2026, 9, 19, 8, tzinfo=timezone.utc),
                planned_finish=datetime(2026, 9, 19, 8, 45, tzinfo=timezone.utc),
                travel_from_previous_min=0,
                waiting_before_job_min=0,
            )
        )
        await session.execute(
            insert(planning_unassigned_jobs).values(
                planning_run_id=run_id,
                job_id=job_ids[1],
                drop_penalty=100,
                primary_reason_code="NOT_ASSIGNED_WITHIN_HORIZON",
                diagnostic_flags={"horizon_end": planning_date.isoformat()},
            )
        )
        cancelled_job_id = await session.scalar(
            insert(jobs)
            .values(
                project_id=project_id,
                internal_code="BOARD-CANCELLED",
                status="CANCELLED",
                address="Старый адрес отменённой заявки",
                sla_date=planning_date,
                work_type_id=work_type_id,
                service_duration_min=30,
                cancelled_at=datetime(2026, 9, 19, 7, tzinfo=timezone.utc),
            )
            .returning(jobs.c.id)
        )
        await session.execute(
            insert(planning_cancelled_job_snapshots).values(
                plan_version_id=version_id,
                project_id=project_id,
                job_id=cancelled_job_id,
                planning_date=planning_date,
                engineer_id=engineer_id,
                previous_sequence=2,
                planned_start=datetime(2026, 9, 19, 9, tzinfo=timezone.utc),
                planned_finish=datetime(2026, 9, 19, 9, 30, tzinfo=timezone.utc),
                cancelled_at=datetime(2026, 9, 19, 7, tzinfo=timezone.utc),
                job_snapshot={
                    "id": int(cancelled_job_id),
                    "address": "Старый адрес отменённой заявки",
                    "work_type_name": "Диагностика линии",
                    "service_duration_min": 30,
                    "sla_date": planning_date.isoformat(),
                    "priority": "LOW",
                    "status": "CANCELLED",
                },
            )
        )
        stale_cancelled_job_id = await session.scalar(
            insert(jobs)
            .values(
                project_id=project_id,
                internal_code="BOARD-STALE-CANCELLED",
                status="CANCELLED",
                address="Отмена из прошлой версии",
                sla_date=planning_date,
                work_type_id=work_type_id,
                service_duration_min=30,
                cancelled_at=datetime(2026, 9, 19, 6, tzinfo=timezone.utc),
            )
            .returning(jobs.c.id)
        )
        await session.execute(
            insert(planning_cancelled_job_snapshots).values(
                plan_version_id=previous_version_id,
                project_id=project_id,
                job_id=stale_cancelled_job_id,
                planning_date=planning_date,
                engineer_id=engineer_id,
                previous_sequence=3,
                cancelled_at=datetime(2026, 9, 19, 6, tzinfo=timezone.utc),
                job_snapshot={
                    "id": int(stale_cancelled_job_id),
                    "address": "Отмена из прошлой версии",
                    "work_type_name": "Диагностика линии",
                    "service_duration_min": 30,
                    "sla_date": planning_date.isoformat(),
                    "priority": "LOW",
                    "status": "CANCELLED",
                },
            )
        )
        equipment_type_id = await session.scalar(
            insert(equipment_types)
            .values(
                project_id=project_id,
                code="NEW-TOOL",
                name="Новое оборудование",
                available_units=5,
            )
            .returning(equipment_types.c.id)
        )
        await session.execute(
            insert(work_type_required_equipment).values(
                work_type_id=work_type_id,
                equipment_type_id=equipment_type_id,
            )
        )
        await session.execute(
            update(work_types)
            .where(work_types.c.id == work_type_id)
            .values(name="Изменённое текущее название")
        )
        await session.commit()

        repository = SqlaDynamicPlanningRepository(session)
        board = await repository.get_planning_board_summary(
            project_id, planning_date, 7
        )
        day = board["selected_day"]
        explanation = await repository.get_planning_job_explanation(
            project_id, int(day_result_id), job_ids[0]
        )
        another_project_id = await seed_project(session)
        foreign_explanation = await repository.get_planning_job_explanation(
            another_project_id, int(day_result_id), job_ids[0]
        )
        comparison = await repository.get_plan_version(project_id, int(version_id))

    assert day["counts"] == {"assigned": 1, "unassigned": 1, "cancelled": 1}
    assert day["engineer_columns"][0]["name"] == "Иван Петров"
    assert day["engineer_columns"][0]["transport_type"] == "CAR"
    assert day["engineer_columns"][0]["start_address"] == "База"
    assert day["engineer_columns"][0]["start_coordinate"] == {
        "latitude": "58.010455",
        "longitude": "56.229443",
    }
    assert day["engineer_columns"][0]["jobs"][0]["job_id"] == job_ids[0]
    assert day["engineer_columns"][0]["jobs"][0]["coordinate"] == {
        "latitude": "58.01",
        "longitude": "56.23",
    }
    assert day["engineer_columns"][0]["jobs"][0]["work_type"] == ("Диагностика линии")
    assert day["engineer_columns"][0]["jobs"][0]["required_equipment"] == []
    assert day["engineer_columns"][0]["cancelled_jobs"][0]["job_id"] == (
        cancelled_job_id
    )
    assert day["engineer_columns"][0]["active_count"] == 1
    assert day["unassigned"]["horizon"][0]["job_id"] == job_ids[1]
    assert day["unassigned"]["horizon"][0]["primary_reason"]["code"] == (
        "HORIZON_EXHAUSTED"
    )
    assert day["unassigned"]["horizon"][0]["primary_reason"]["parameters"] == {
        "horizon_end": planning_date.isoformat()
    }
    assert explanation is not None
    assert explanation["outcome"] == "ASSIGNED"
    assert explanation["eligible_engineers_count"] == 1
    assert foreign_explanation is None
    assert board["plan_version"]["number"] == 2
    assert board["plan_version"]["status"] == "SUCCESS"
    assert day["day_result_id"] == day_result_id
    assert comparison is not None
    assert comparison["comparison"]["metrics"]["current"]["days"][0] == {
        "planning_date": planning_date.isoformat(),
        "personnel_count": 1,
        "assigned_jobs_count": 1,
        "distance_meters": 0,
        "engineers": [
            {
                "engineer_id": int(engineer_id),
                "engineer_name": "Иван Петров",
                "assigned_jobs_count": 1,
                "distance_meters": 0,
            }
        ],
    }
    assert comparison["comparison"]["input_changes"]["snapshot_available"] is True
    assert comparison["comparison"]["triggers"][0]["event_type"] == "JOB_CREATED"
    assert comparison["comparison"]["triggers"][0]["job_ids"] == [job_ids[0]]


async def test_planning_board_explains_zero_equipment_without_a_daily_run(
    pg_engine: AsyncEngine,
) -> None:
    planning_date = date(2026, 9, 24)
    async with AsyncSession(pg_engine, expire_on_commit=False) as session:
        project_id = await seed_project(session)
        work_type_id = int(
            await session.scalar(
                insert(work_types)
                .values(
                    project_id=project_id,
                    code="LADDER-WORK",
                    name="Высотные работы",
                    default_service_duration_min=60,
                )
                .returning(work_types.c.id)
            )
        )
        equipment_id = int(
            await session.scalar(
                insert(equipment_types)
                .values(
                    project_id=project_id,
                    code="LADDER",
                    name="Лестница",
                    available_units=0,
                )
                .returning(equipment_types.c.id)
            )
        )
        await session.execute(
            insert(work_type_required_equipment).values(
                work_type_id=work_type_id,
                equipment_type_id=equipment_id,
            )
        )
        job_id = int(
            await session.scalar(
                insert(jobs)
                .values(
                    project_id=project_id,
                    internal_code="ZERO-LADDER",
                    status="NEW",
                    address="Ленина, 10",
                    latitude=58.01,
                    longitude=56.23,
                    sla_date=planning_date,
                    work_type_id=work_type_id,
                    service_duration_min=60,
                )
                .returning(jobs.c.id)
            )
        )
        snapshot = {
            "project": {
                "maximum_horizon_end": planning_date.isoformat(),
            },
            "jobs": [
                {
                    "id": job_id,
                    "work_type_id": work_type_id,
                    "address": "Ленина, 10",
                    "sla_date": planning_date.isoformat(),
                    "service_duration_min": 60,
                    "priority": "LOW",
                    "status": "NEW",
                }
            ],
            "engineers": [],
            "schedules": [],
            "required_equipment": {str(work_type_id): [equipment_id]},
            "required_qualifications": {str(work_type_id): []},
            "equipment_units": {str(equipment_id): 0},
            "equipment_types": [
                {"id": equipment_id, "name": "Лестница", "available_units": 0}
            ],
        }
        batch_id = int(
            await session.scalar(
                insert(planning_batches)
                .values(
                    project_id=project_id,
                    requested_start_date=planning_date,
                    effective_start_date=planning_date,
                    initial_horizon_end=planning_date,
                    maximum_horizon_end=planning_date,
                    status="PARTIAL",
                    completion_reason="NO_FUTURE_OPPORTUNITIES",
                    idempotency_key="zero-equipment-board",
                    input_hash="zero-equipment-snapshot",
                    configuration_version="1",
                    input_snapshot=snapshot,
                )
                .returning(planning_batches.c.id)
            )
        )
        previous_version_id = int(
            await session.scalar(
                insert(project_plan_versions)
                .values(
                    project_id=project_id,
                    version_number=1,
                    planning_batch_id=batch_id,
                    input_hash="previous-zero-equipment-plan",
                    trigger_source="MANUAL",
                    is_current=False,
                    unassigned_jobs=[
                        {
                            "job_id": job_id,
                            "primary_reason_code": "NO_SHIFT_IN_HORIZON",
                            "diagnostic_flags": {},
                        }
                    ],
                    superseded_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
                )
                .returning(project_plan_versions.c.id)
            )
        )
        version_id = int(
            await session.scalar(
                insert(project_plan_versions)
                .values(
                    project_id=project_id,
                    version_number=2,
                    planning_batch_id=batch_id,
                    input_hash="zero-equipment-plan",
                    trigger_source="MANUAL",
                    is_current=True,
                    unassigned_jobs=[
                        {
                            "job_id": job_id,
                            "primary_reason_code": ("EQUIPMENT_UNAVAILABLE_IN_HORIZON"),
                            "diagnostic_flags": {"equipment_type_ids": [equipment_id]},
                        }
                    ],
                )
                .returning(project_plan_versions.c.id)
            )
        )
        await session.commit()

        repository = SqlaDynamicPlanningRepository(session)
        board = await repository.get_planning_board_summary(
            project_id, planning_date, 7
        )
        comparison = await repository.get_plan_version(project_id, version_id)

    card = board["selected_day"]["unassigned"]["horizon"][0]
    assert card["job_id"] == job_id
    assert card["required_equipment"] == [{"id": equipment_id, "name": "Лестница"}]
    assert card["primary_reason"]["code"] == "NO_EQUIPMENT"
    assert card["primary_reason"]["text"] == (
        "Недоступно обязательное оборудование: Лестница (доступно: 0)"
    )
    assert previous_version_id != version_id
    assert comparison is not None
    assert comparison["comparison"]["previous_version_number"] == 1
    assert comparison["changes"] == []
    assert comparison["comparison"]["changed_count"] == 1
    unassigned_change = comparison["comparison"]["unassigned_changes"][0]
    assert unassigned_change["previous_reason"]["code"] == "NO_SHIFT"
    assert unassigned_change["current_reason"]["text"] == (
        "Недоступно обязательное оборудование: Лестница (доступно: 0)"
    )


async def test_concurrent_manual_planning_starts_reuse_one_event(
    pg_engine: AsyncEngine,
) -> None:
    async with AsyncSession(pg_engine, expire_on_commit=False) as session:
        project_id = await seed_project(session)
        actor_id = uuid.uuid4()
        await session.execute(
            insert(users_table).values(
                **owner_values(actor_id, f"owner-{uuid.uuid4().hex}")
            )
        )
        await session.commit()

    gate = asyncio.Event()
    ready = 0
    ready_lock = asyncio.Lock()

    async def enqueue_manual(suffix: str) -> dict:
        nonlocal ready
        async with AsyncSession(pg_engine, expire_on_commit=False) as session:
            repository = SqlaDynamicPlanningRepository(session)
            async with ready_lock:
                ready += 1
                if ready == 2:
                    gate.set()
            await gate.wait()
            result = await repository.enqueue(
                project_id,
                "MANUAL",
                actor_id,
                f"manual-{suffix}",
            )
            await session.commit()
            return result

    results = await asyncio.gather(enqueue_manual("a"), enqueue_manual("b"))

    assert results[0]["id"] == results[1]["id"]
    assert sorted(result["_reused_active"] for result in results) == [False, True]
    async with pg_engine.connect() as connection:
        assert (
            await connection.scalar(
                select(func.count())
                .select_from(planning_events)
                .where(planning_events.c.project_id == project_id)
            )
            == 1
        )
