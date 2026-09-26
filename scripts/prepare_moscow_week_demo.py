"""Create an empty, isolated Moscow project for the 70-job CSV demo.

The user imports ``data/demo_moscow_70_jobs_2026-09-27.csv`` separately.
This script never inserts jobs or modifies existing projects.
"""

import argparse
import asyncio
import json
from datetime import date, time, timedelta

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from auth.domain.user_role import UserRoleEnum
from auth.entrypoint.config import PostgresConfig
from auth.infrastructure.persistence_sqla.mappings.user import users_table
from planning.infrastructure.persistence_sqla.mappings.tables import (
    dispatcher_projects,
    engineer_qualifications,
    engineer_schedules,
    engineers,
    equipment_types,
    planning_config,
    projects,
    qualifications,
    work_type_required_equipment,
    work_type_required_qualifications,
    work_types,
)

PROJECT_CODE = "DEMO-MOSCOW-70-WEEK-20260927"
PROJECT_NAME = "Демо Москва · 70 заявок · неделя"
FIRST_DAY = date(2026, 9, 27)

# All six starting points and the 25 CSV job points belong to the same
# connected automobile graph in the locally installed Moscow Valhalla tiles.
ENGINEER_BASES = (
    ("Инженер Центр", "Россия, Москва, Тверская улица, 13", 55.761350, 37.609056),
    ("Инженер Восток", "Россия, Москва, Хабаровская улица, 9", 55.818923, 37.825137),
    (
        "Инженер Запад",
        "Россия, Москва, Кутузовский проспект, 10",
        55.748650,
        37.558580,
    ),
    (
        "Инженер Юг",
        "Россия, Москва, Варшавское шоссе, 10к1",
        55.698173,
        37.619836,
    ),
    (
        "Инженер Север",
        "Россия, Москва, Ленинградский проспект, 33к1",
        55.786480,
        37.560457,
    ),
    (
        "Инженер Юго-восток",
        "Россия, Москва, Волгоградский проспект, 10",
        55.730127,
        37.673241,
    ),
)


async def prepare(dispatcher_login: str) -> dict:
    engine = create_async_engine(PostgresConfig.from_env().uri)
    try:
        async with engine.begin() as connection:
            existing = await connection.scalar(
                select(projects.c.id).where(projects.c.internal_code == PROJECT_CODE)
            )
            if existing is not None:
                return {"project_id": existing, "name": PROJECT_NAME, "created": False}

            dispatcher = (
                (
                    await connection.execute(
                        select(
                            users_table.c.id,
                            users_table.c.role,
                            users_table.c.is_active,
                        ).where(
                            users_table.c.username == dispatcher_login
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                dispatcher is None
                or dispatcher["role"] is not UserRoleEnum.DISPATCHER
                or not dispatcher["is_active"]
            ):
                raise ValueError(f"Active dispatcher not found: {dispatcher_login}")

            async def add(table, **values):
                return await connection.scalar(
                    insert(table).values(**values).returning(table.c.id)
                )

            project_id = await add(
                projects,
                name=PROJECT_NAME,
                internal_code=PROJECT_CODE,
                planning_timezone="Europe/Moscow",
            )
            await connection.execute(
                insert(dispatcher_projects).values(
                    user_id=dispatcher["id"], project_id=project_id
                )
            )
            await add(
                planning_config,
                project_id=project_id,
                version=1,
                solver_time_limit_sec=60,
                candidate_solver_time_limit_sec=15,
                event_time_limit_sec=900,
                event_coalesce_window_sec=1,
                event_coalesce_max_wait_sec=3,
                travel_provider="VALHALLA_LOCAL",
                nightly_planning_enabled=False,
            )
            qualification_ids = [
                await add(
                    qualifications,
                    project_id=project_id,
                    code=code,
                    name=name,
                )
                for code, name in (
                    ("NETWORK", "Сети и диагностика"),
                    ("INSTALL", "Монтаж оборудования"),
                )
            ]
            equipment_id = await add(
                equipment_types,
                project_id=project_id,
                code="TESTER",
                name="Оптический тестер",
                available_units=6,
            )
            work_type_ids = []
            for code, name, priority, duration in (
                ("CONNECT", "Подключение", "MEDIUM", 60),
                ("DIAG", "Диагностика", "HIGH", 65),
                ("REPAIR", "Ремонт линии", "CRITICAL", 75),
            ):
                work_type_id = await add(
                    work_types,
                    project_id=project_id,
                    code=code,
                    name=name,
                    priority=priority,
                    default_service_duration_min=duration,
                    required_transport="CAR",
                )
                work_type_ids.append(work_type_id)
                for qualification_id in qualification_ids:
                    await connection.execute(
                        insert(work_type_required_qualifications).values(
                            work_type_id=work_type_id,
                            qualification_id=qualification_id,
                        )
                    )
                if code != "CONNECT":
                    await connection.execute(
                        insert(work_type_required_equipment).values(
                            work_type_id=work_type_id,
                            equipment_type_id=equipment_id,
                        )
                    )

            engineer_ids = []
            for index, (name, address, latitude, longitude) in enumerate(
                ENGINEER_BASES, start=1
            ):
                engineer_id = await add(
                    engineers,
                    project_id=project_id,
                    internal_code=f"DEMO-MOSCOW-70-ENG-{index}",
                    name=name,
                    active=True,
                    transport_type="CAR",
                    start_address=address,
                    start_latitude=latitude,
                    start_longitude=longitude,
                )
                engineer_ids.append(engineer_id)
                await connection.execute(
                    insert(engineer_qualifications),
                    [
                        {"engineer_id": engineer_id, "qualification_id": value}
                        for value in qualification_ids
                    ],
                )
                await connection.execute(
                    insert(engineer_schedules),
                    [
                        {
                            "engineer_id": engineer_id,
                            "work_date": FIRST_DAY + timedelta(days=offset),
                            "shift_start": time(8),
                            "shift_end": time(18),
                        }
                        for offset in range(30)
                    ],
                )

            return {
                "project_id": project_id,
                "name": PROJECT_NAME,
                "created": True,
                "dispatcher_login": dispatcher_login,
                "engineers": len(engineer_ids),
                "work_types": len(work_type_ids),
                "qualifications": len(qualification_ids),
                "equipment_types": 1,
                "shifts": len(engineer_ids) * 30,
                "jobs": 0,
            }
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dispatcher-login", default="disas")
    arguments = parser.parse_args()
    result = asyncio.run(prepare(arguments.dispatcher_login))
    print(json.dumps(result, ensure_ascii=False))
