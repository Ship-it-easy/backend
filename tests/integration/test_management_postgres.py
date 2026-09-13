import asyncio
import os
import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, insert, select, text
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
from planning.infrastructure.adapters.planning_management_repository_sqla import (
    SqlaPlanningManagementRepository,
)
from planning.infrastructure.adapters.project_management_repositories_sqla import (
    SqlaEngineerAccountRepository,
)
from planning.infrastructure.adapters.unit_of_work_sqla import SqlaPlanningUnitOfWork
from planning.infrastructure.persistence_sqla.mappings.tables import (
    assignments,
    daily_plans,
    engineers,
    jobs,
    plan_versions,
    planning_route_jobs,
    planning_routes,
    planning_runs,
    projects,
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
                SqlaPlanningUnitOfWork(session),
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
    user.update(role=UserRoleEnum.DISPATCHER, project_id=project_id)
    await session.execute(insert(users_table).values(**user))
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
        access, repository, SqlaPlanningUnitOfWork(session)
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
