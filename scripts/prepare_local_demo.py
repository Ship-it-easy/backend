"""Create a separate, repeatable local demo. Never edits other projects.

Run: .venv/bin/python scripts/prepare_local_demo.py [--date YYYY-MM-DD]
Credentials and IDs are printed for the newly created demo only.
"""

import argparse
import asyncio
import json
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import bcrypt
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
    jobs,
    planning_config,
    projects,
    qualifications,
    work_type_required_equipment,
    work_type_required_qualifications,
    work_types,
)

# Coordinates verified against the locally installed Nominatim dataset.
LOCATIONS = [
    ("Пермь, Ленина, 50", 58.011905, 56.242396),
    ("Пермь, Комсомольский проспект, 27", 58.009556, 56.239246),
    ("Пермь, Екатерининская, 48", 58.013210, 56.253553),
    ("Пермь, Куйбышева, 36", 58.007656, 56.236480),
    ("Пермь, Петропавловская, 25", 58.015079, 56.244518),
    ("Пермь, Монастырская, 12", 58.015679, 56.233412),
]

# Two deliberately separated city clusters. Jobs alternate between them in
# FIFO order, while the optimizer can keep each car inside its own cluster.
# This makes the comparison visually obvious without changing either
# algorithm or fabricating metrics.
BASELINE_SHOWCASE_LOCATIONS = [
    ("Москва, Тверская улица, 13", 55.761350, 37.609056),
    ("Москва, Хабаровская улица, 9", 55.818923, 37.825137),
    ("Москва, Тверская улица, 31", 55.769008, 37.596534),
    ("Москва, Хабаровская улица, 9", 55.818923, 37.825137),
    ("Москва, Тверская улица, 15", 55.762373, 37.607898),
    ("Москва, Хабаровская улица, 9", 55.818923, 37.825137),
    ("Москва, Тверская улица, 13", 55.761350, 37.609056),
    ("Москва, Хабаровская улица, 9", 55.818923, 37.825137),
]


async def prepare(
    day: date, suffix: str = "", *, baseline_showcase: bool = False
) -> dict:
    stamp = day.strftime("%Y%m%d") + suffix + ("-fifo" if baseline_showcase else "")
    manifest_path = Path(f"/private/tmp/routes-demo-{stamp}.json")
    engine = create_async_engine(PostgresConfig.from_env().uri)
    try:
        async with engine.begin() as connection:
            existing = await connection.scalar(
                select(projects.c.id).where(projects.c.internal_code == f"DEMO-{stamp}")
            )
            if existing is not None:
                if manifest_path.exists():
                    return json.loads(manifest_path.read_text())
                raise RuntimeError(
                    "Demo already exists; existing data and passwords are preserved. "
                    "Use another --suffix."
                )

            async def add(table, **values):
                return await connection.scalar(
                    insert(table).values(**values).returning(table.c.id)
                )

            project_id = await add(
                projects,
                name=(
                    f"Демо FIFO vs оптимизатор · {day.isoformat()}{suffix}"
                    if baseline_showcase
                    else f"Демо маршрутов · {day.isoformat()}{suffix}"
                ),
                internal_code=f"DEMO-{stamp}",
                planning_timezone="Asia/Yekaterinburg",
            )
            await add(
                planning_config,
                project_id=project_id,
                version=1,
                solver_time_limit_sec=3,
                candidate_solver_time_limit_sec=3,
                event_coalesce_window_sec=1,
                event_coalesce_max_wait_sec=3,
                event_time_limit_sec=300,
                travel_provider="VALHALLA_LOCAL",
                nightly_planning_enabled=False,
            )
            skill_id = await add(
                qualifications,
                project_id=project_id,
                code="NETWORK",
                name="Сети и диагностика",
            )
            equipment_id = await add(
                equipment_types,
                project_id=project_id,
                code="TESTER",
                name="Оптический тестер",
                available_units=1,
            )
            install_id = await add(
                work_types,
                project_id=project_id,
                code="INSTALL",
                name="Подключение интернета",
                priority="LOW",
                default_service_duration_min=90,
                required_transport="CAR",
            )
            repair_id = await add(
                work_types,
                project_id=project_id,
                code="REPAIR",
                name="Диагностика линии",
                priority="CRITICAL",
                default_service_duration_min=60,
            )
            for work_id in (install_id, repair_id):
                await connection.execute(
                    insert(work_type_required_qualifications).values(
                        work_type_id=work_id, qualification_id=skill_id
                    )
                )
            await connection.execute(
                insert(work_type_required_equipment).values(
                    work_type_id=repair_id, equipment_type_id=equipment_id
                )
            )
            engineer_ids = []
            engineer_locations = (
                [
                    BASELINE_SHOWCASE_LOCATIONS[0],
                    BASELINE_SHOWCASE_LOCATIONS[1],
                    BASELINE_SHOWCASE_LOCATIONS[2],
                ]
                if baseline_showcase
                else LOCATIONS
            )
            for i, (name, transport) in enumerate(
                (
                    ("Алексей · авто", "CAR"),
                    ("Мария · авто", "CAR"),
                    ("Иван · пешком", "NONE"),
                )
            ):
                address, lat, lon = engineer_locations[i]
                engineer_id = await add(
                    engineers,
                    project_id=project_id,
                    internal_code=f"DEMO-ENG-{stamp}-{i}",
                    name=name,
                    active=True,
                    transport_type=transport,
                    start_address=address,
                    start_latitude=lat,
                    start_longitude=lon,
                )
                engineer_ids.append(engineer_id)
                await connection.execute(
                    insert(engineer_qualifications).values(
                        engineer_id=engineer_id, qualification_id=skill_id
                    )
                )
                await connection.execute(
                    insert(engineer_schedules),
                    [
                        dict(
                            engineer_id=engineer_id,
                            work_date=day + timedelta(days=offset),
                            shift_start=(
                                time(0)
                                if baseline_showcase and offset == 0
                                else time(8)
                            ),
                            shift_end=(
                                time(0, 1)
                                if baseline_showcase and offset == 0
                                else time(16)
                            ),
                        )
                        for offset in range(30)
                    ],
                )
            job_ids = []
            job_count = 8 if baseline_showcase else 14
            job_locations = (
                BASELINE_SHOWCASE_LOCATIONS if baseline_showcase else LOCATIONS
            )
            received_at = datetime.now(ZoneInfo("Asia/Yekaterinburg"))
            for i in range(job_count):
                address, lat, lon = job_locations[i % len(job_locations)]
                work_id = (
                    install_id
                    if baseline_showcase
                    else repair_id
                    if i in (1, 5, 9, 13)
                    else install_id
                )
                job_ids.append(
                    await add(
                        jobs,
                        project_id=project_id,
                        internal_code=f"DEMO-JOB-{stamp}-{i}",
                        external_id=f"ДЕМО-{i + 1:02}",
                        status="NEW",
                        received_at=received_at,
                        ingest_sequence=i + 1,
                        address=address,
                        latitude=lat,
                        longitude=lon,
                        sla_date=(
                            day + timedelta(days=1)
                            if baseline_showcase
                            else day
                            + timedelta(
                                days=(
                                    -1 if i < 2 else 0 if i < 6 else 1 if i < 10 else 3
                                )
                            )
                        ),
                        time_window_start=time(8),
                        time_window_end=time(15),
                        work_type_id=work_id,
                    )
                )
            password = "DemoRoutes2026!"
            password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            accounts = []
            for role, engineer_id, login in [
                (UserRoleEnum.DISPATCHER, None, f"demo_routes_{stamp}"),
                (UserRoleEnum.ENGINEER, engineer_ids[0], f"demo_engineer_{stamp}"),
            ]:
                user_id = uuid.uuid4()
                await connection.execute(
                    insert(users_table).values(
                        id=user_id,
                        username=login,
                        password_hash=password_hash,
                        is_active=True,
                        is_verified=True,
                        role=role,
                        project_id=(
                            project_id if role is UserRoleEnum.ENGINEER else None
                        ),
                        engineer_id=engineer_id,
                    )
                )
                if role is UserRoleEnum.DISPATCHER:
                    await connection.execute(
                        insert(dispatcher_projects).values(
                            user_id=user_id,
                            project_id=project_id,
                        )
                    )
                accounts.append(dict(login=login, password=password, role=role.value))
            result = dict(
                project_id=project_id,
                planning_date=day.isoformat(),
                comparison_date=(
                    day + timedelta(days=1) if baseline_showcase else day
                ).isoformat(),
                engineer_ids=engineer_ids,
                job_ids=job_ids,
                work_type_ids=[install_id, repair_id],
                baseline_showcase=baseline_showcase,
                accounts=accounts,
                manifest=str(manifest_path),
            )
        manifest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        manifest_path.chmod(0o600)
        return result
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=datetime.now(ZoneInfo("Asia/Yekaterinburg")).date(),
    )
    parser.add_argument("--suffix", default="")
    parser.add_argument(
        "--baseline-showcase",
        action="store_true",
        help="Create alternating west/east jobs for a clear FIFO comparison",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                prepare(
                    args.date,
                    args.suffix,
                    baseline_showcase=args.baseline_showcase,
                )
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
