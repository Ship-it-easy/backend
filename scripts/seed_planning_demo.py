"""Create a small idempotent planning demo dataset for Perm."""

import asyncio
from datetime import datetime, time, timedelta, timezone
from os import getenv
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from auth.entrypoint.config import PostgresConfig
from planning.infrastructure.persistence_sqla.mappings.tables import (
    engineer_qualifications,
    engineer_schedules,
    engineers,
    equipment_types,
    jobs,
    planning_config,
    projects,
    qualifications,
    work_type_required_equipment,
    work_type_required_qualifications,
    work_types,
)


async def seed() -> None:
    engine = create_async_engine(PostgresConfig.from_env().uri)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        planning_date = (
            datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Yekaterinburg")).date()
        )
        existing = await session.scalar(
            select(projects.c.id).where(projects.c.name == "Perm Demo")
        )
        if existing is not None:
            await _refresh_demo(session, int(existing), planning_date)
            await session.commit()
            print(f"Perm Demo updated: project_id={existing}, date={planning_date}")
            return
        project_id = await _id(
            session,
            projects,
            internal_code="PRJ-PERM-DEMO",
            name="Perm Demo",
            planning_timezone="Asia/Yekaterinburg",
            planning_one_day_enabled=True,
        )
        qualification_id = await _id(
            session,
            qualifications,
            project_id=project_id,
            code="FIBER",
            name="Fiber installation",
        )
        equipment_id = await _id(
            session,
            equipment_types,
            project_id=project_id,
            code="OTDR",
            name="Optical tester",
            available_units=1,
            active=True,
        )
        install_type_id = await _id(
            session,
            work_types,
            project_id=project_id,
            code="INSTALL",
            name="Internet installation",
            default_service_duration_min=60,
            required_transport="CAR",
        )
        repair_type_id = await _id(
            session,
            work_types,
            project_id=project_id,
            code="REPAIR",
            name="Line diagnostics",
            default_service_duration_min=45,
            required_transport=None,
        )
        await session.execute(
            insert(work_type_required_qualifications),
            [
                {
                    "work_type_id": install_type_id,
                    "qualification_id": qualification_id,
                },
                {
                    "work_type_id": repair_type_id,
                    "qualification_id": qualification_id,
                },
            ],
        )
        await session.execute(
            insert(work_type_required_equipment).values(
                work_type_id=repair_type_id,
                equipment_type_id=equipment_id,
            )
        )
        engineer_ids = []
        for name, transport, latitude, longitude in (
            ("Alexey", "CAR", 58.0105, 56.2502),
            ("Maria", "NONE", 58.0650, 55.6300),
            ("Ivan", "NONE", 58.0200, 56.2200),
        ):
            engineer_ids.append(
                await _id(
                    session,
                    engineers,
                    project_id=project_id,
                    internal_code=f"ENG-PERM-{len(engineer_ids) + 1}",
                    name=name,
                    active=True,
                    transport_type=transport,
                    start_address=("Пермь, Автозаводская, 26" if name == "Alexey" else "Пермь, Закамск" if name == "Maria" else "Пермь, улица Куйбышева"),
                    start_latitude=latitude,
                    start_longitude=longitude,
                )
            )
        for engineer_id in engineer_ids:
            await session.execute(
                insert(engineer_schedules).values(
                    engineer_id=engineer_id,
                    work_date=planning_date,
                    shift_start=time(0, 0),
                    shift_end=time(23, 59),
                )
            )
            await session.execute(
                insert(engineer_qualifications).values(
                    engineer_id=engineer_id,
                    qualification_id=qualification_id,
                )
            )
        now = datetime.now(timezone.utc)
        job_values = []
        for number, (address, work_type_id) in enumerate(
            (
                ("Пермь, улица Ленина, 58", install_type_id),
                ("Пермь, Комсомольский проспект, 27", repair_type_id),
            ("Пермь, улица Екатерининская, 75", repair_type_id),
            ("Пермь, улица Сибирская, 35", install_type_id),
            ("Пермь, улица Куйбышева, 10", repair_type_id),
            ("Пермь, улица Куйбышева, 18", install_type_id),
            ("улица Куйбышева, Пермь", repair_type_id),
            ("Автозаводская улица, Закамск, Кировский район, Пермь", repair_type_id),
            ),
            start=1,
        ):
            job_values.append(
                {
                    "project_id": project_id,
                    "external_id": f"PERM-{number}",
                    "internal_code": f"JOB-PERM-{number}",
                    "status": "NEW",
                    "address": address,
                    "latitude": None,
                    "longitude": None,
                    "sla_date": planning_date + timedelta(days=0 if number in (1, 2, 7, 8) else 1),
                    "work_type_id": work_type_id,
                    "service_duration_min": 60 if work_type_id == install_type_id else 45,
                    "created_at": now + timedelta(seconds=number),
                    "updated_at": now,
                }
            )
        await session.execute(insert(jobs), job_values)
        await session.execute(
            insert(planning_config).values(
                project_id=project_id,
                version=1,
                active=True,
                travel_provider=getenv("DEMO_TRAVEL_PROVIDER", "VALHALLA_LOCAL"),
                solver_time_limit_sec=60,
                max_jobs_per_run=1000,
            )
        )
        await session.commit()
        print(f"Created Perm Demo: project_id={project_id}, date={planning_date}")
    await engine.dispose()


async def _refresh_demo(session: AsyncSession, project_id: int, planning_date) -> None:
    """Apply the current demo fixtures to an existing Perm Demo project."""
    engineer_rows = (
        await session.execute(
            select(engineers.c.id).where(engineers.c.project_id == project_id)
        )
    ).all()
    for row in engineer_rows:
        await session.execute(
            update(engineer_schedules)
            .where(
                engineer_schedules.c.engineer_id == row.id,
                engineer_schedules.c.work_date == planning_date,
            )
            .values(shift_start=time(0, 0), shift_end=time(23, 59))
        )

    addresses = {
        "PERM-1": "Пермь, улица Ленина, 58",
        "PERM-2": "Пермь, Комсомольский проспект, 27",
        "PERM-3": "Пермь, улица Екатерининская, 75",
        "PERM-4": "Пермь, улица Сибирская, 35",
    }
    for external_id, address in addresses.items():
        await session.execute(
            update(jobs)
            .where(
                jobs.c.project_id == project_id,
                jobs.c.external_id == external_id,
            )
            .values(address=address, latitude=None, longitude=None)
        )
    await session.execute(
        update(equipment_types)
        .where(equipment_types.c.project_id == project_id)
        .values(available_units=1)
    )
    await session.execute(
        update(planning_config)
        .where(
            planning_config.c.project_id == project_id,
            planning_config.c.active.is_(True),
        )
        .values(
            travel_provider=getenv("DEMO_TRAVEL_PROVIDER", "VALHALLA_LOCAL"),
            max_jobs_per_run=1000,
        )
    )


async def _id(session: AsyncSession, table, **values) -> int:
    return int(
        await session.scalar(insert(table).values(**values).returning(table.c.id))
    )


if __name__ == "__main__":
    asyncio.run(seed())
