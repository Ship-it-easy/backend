"""Create an isolated Moscow demo with the three supported engineer transport types.

Run inside the backend container after migrations. The generated login is saved
in a mode-0600 manifest under /tmp and printed once for the local operator.
"""

import argparse
import asyncio
import json
import secrets
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
    jobs,
    planning_config,
    projects,
    qualifications,
    work_type_required_qualifications,
    work_types,
)

# The points are inside the local Moscow OSM graph.
PROFILES = (
    (
        "CAR",
        "Автомобиль",
        ("Киевский вокзал", 55.743673, 37.566101),
        (
            ("Бережковская набережная", 55.738795, 37.553240),
            ("Смоленская площадь", 55.747855, 37.581245),
        ),
    ),
    (
        "NONE",
        "Пешком",
        ("Парк культуры", 55.735917, 37.594836),
        (
            ("Зубовский бульвар", 55.733478, 37.587018),
            ("Крымский мост", 55.732092, 37.600484),
        ),
    ),
    (
        "BICYCLE",
        "Велосипед",
        ("Сокольники", 55.789224, 37.677204),
        (
            ("Русаковская улица", 55.783680, 37.672580),
            ("Преображенская площадь", 55.794706, 37.713076),
        ),
    ),

)


async def prepare(day: date) -> dict:
    stamp = day.strftime("%Y%m%d")
    manifest_path = Path(f"/tmp/transport-demo-{stamp}.json")
    engine = create_async_engine(PostgresConfig.from_env().uri)
    try:
        async with engine.begin() as connection:
            existing = await connection.scalar(
                select(projects.c.id).where(
                    projects.c.internal_code == f"DEMO-TRANSPORT-{stamp}"
                )
            )
            if existing is not None:
                if manifest_path.exists():
                    return json.loads(manifest_path.read_text(encoding="utf-8"))
                raise RuntimeError(
                    "Demo project exists but its login manifest is missing; "
                    "existing accounts will not be changed."
                )

            async def add(table, **values):
                return await connection.scalar(
                    insert(table).values(**values).returning(table.c.id)
                )

            project_id = await add(
                projects,
                name=f"Демо транспорта · {day.isoformat()}",
                internal_code=f"DEMO-TRANSPORT-{stamp}",
                planning_timezone="Europe/Moscow",
            )
            await add(
                planning_config,
                project_id=project_id,
                version=1,
                solver_time_limit_sec=15,
                candidate_solver_time_limit_sec=15,
                event_coalesce_window_sec=1,
                event_coalesce_max_wait_sec=3,
                event_time_limit_sec=300,
                travel_provider="VALHALLA_LOCAL",
                nightly_planning_enabled=False,
            )
            engineer_ids = []
            job_ids = []
            first_schedule_day = min(
                day, datetime.now(ZoneInfo("Europe/Moscow")).date()
            )
            schedule_days = (day + timedelta(days=6) - first_schedule_day).days + 1
            for profile, label, start, destinations in PROFILES:
                skill_id = await add(
                    qualifications,
                    project_id=project_id,
                    code=f"DEMO_{profile}",
                    name=f"Демо: {label}",
                )
                work_type_id = await add(
                    work_types,
                    project_id=project_id,
                    code=f"DEMO_{profile}",
                    name=f"Демо-заявка: {label}",
                    priority="LOW",
                    default_service_duration_min=40,
                    required_transport="CAR" if profile == "CAR" else None,
                )
                await connection.execute(
                    insert(work_type_required_qualifications).values(
                        work_type_id=work_type_id, qualification_id=skill_id
                    )
                )
                engineer_id = await add(
                    engineers,
                    project_id=project_id,
                    internal_code=f"DEMO-{stamp}-{profile}",
                    name=f"Инженер · {label}",
                    active=True,
                    transport_type=profile,
                    start_address=f"Москва, {start[0]}",
                    start_latitude=start[1],
                    start_longitude=start[2],
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
                        {
                            "engineer_id": engineer_id,
                            "work_date": first_schedule_day + timedelta(days=offset),
                            "shift_start": (
                                time(23)
                                if first_schedule_day + timedelta(days=offset) < day
                                else time(8)
                            ),
                            "shift_end": (
                                time(23, 59)
                                if first_schedule_day + timedelta(days=offset) < day
                                else time(18)
                            ),
                        }
                        for offset in range(schedule_days)
                    ],
                )
                for index, (address, lat, lon) in enumerate(destinations, 1):
                    job_ids.append(
                        await add(
                            jobs,
                            project_id=project_id,
                            internal_code=f"DEMO-{stamp}-{profile}-{index}",
                            external_id=f"{label} · {index}",
                            status="NEW",
                            address=f"Москва, {address}",
                            latitude=lat,
                            longitude=lon,
                            sla_date=day,
                            time_window_start=time(8),
                            time_window_end=time(17),
                            work_type_id=work_type_id,
                        )
                    )

            login = f"demo_transport_{stamp}"
            password = secrets.token_urlsafe(12)
            user_id = uuid.uuid4()
            await connection.execute(
                insert(users_table).values(
                    id=user_id,
                    username=login,
                    password_hash=bcrypt.hashpw(
                        password.encode(), bcrypt.gensalt()
                    ).decode(),
                    is_active=True,
                    is_verified=True,
                    role=UserRoleEnum.DISPATCHER,
                    project_id=None,
                    engineer_id=None,
                )
            )
            await connection.execute(
                insert(dispatcher_projects).values(
                    user_id=user_id, project_id=project_id
                )
            )
            result = {
                "project_id": project_id,
                "planning_date": day.isoformat(),
                "engineer_ids": engineer_ids,
                "job_ids": job_ids,
                "accounts": [
                    {
                        "login": login,
                        "password": password,
                        "role": UserRoleEnum.DISPATCHER.value,
                    }
                ],
                "manifest": str(manifest_path),
            }
        manifest_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        manifest_path.chmod(0o600)
        return result
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=datetime.now(ZoneInfo("Europe/Moscow")).date(),
    )
    print(
        json.dumps(asyncio.run(prepare(parser.parse_args().date)), ensure_ascii=False)
    )
