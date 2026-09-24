import asyncio
from datetime import date
from typing import Literal

import httpx
from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, HTTPException
from pydantic import AwareDatetime, BaseModel, Field

from planning.application.access import ProjectAccess
from planning.domain.traffic import (
    INTERVAL_MINUTES,
    MOSCOW,
    QUALITY,
    SERVICE_BOUNDS,
    VERSION,
    profile_table,
)
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.traffic_route import build_traffic_route

router = APIRouter()


class TrafficStop(BaseModel):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    job_id: int | None = None
    service_seconds: int = Field(default=0, ge=0, le=86400)
    not_before: AwareDatetime | None = None
    planned_start: AwareDatetime | None = None
    access_seconds: int = Field(default=0, ge=0, le=7200)


class TrafficRouteRequest(BaseModel):
    departure_at: AwareDatetime
    profile: Literal["auto", "pedestrian"] = "auto"
    stops: list[TrafficStop] = Field(min_length=2, max_length=100)
    include_departure_options: bool = True


@router.get("/traffic/model")
@inject
async def traffic_model(
    access: FromDishka[ProjectAccess],
    config: FromDishka[PlanningServiceConfig],
) -> dict:
    await access.dispatcher()
    return {
        "model_version": VERSION,
        "enabled": config.traffic_model_enabled,
        "quality": QUALITY,
        "interval_minutes": INTERVAL_MINUTES,
        "coverage_bounds": SERVICE_BOUNDS,
        "areas": ["Москва", "Домодедово", "Ступино", "Кашира", "Связующие дороги"],
        "accuracy": {
            "status": "not_validated",
            "accuracy_percent": None,
            "mae_minutes": None,
            "observed_trip_count": 0,
        },
        "description": "Точность не измерена: необходимы фактические времена поездок.",
    }


@router.get("/traffic/profiles")
@inject
async def traffic_profiles(
    access: FromDishka[ProjectAccess],
    day: date,
) -> dict:
    await access.dispatcher()
    return profile_table(day)


@router.post("/traffic/route")
@inject
async def traffic_route(
    body: TrafficRouteRequest,
    access: FromDishka[ProjectAccess],
    config: FromDishka[PlanningServiceConfig],
) -> dict:
    await access.dispatcher()
    # Valhalla interprets date_time as local to the origin, not as UTC.
    body.departure_at = body.departure_at.astimezone(MOSCOW)
    try:
        async with asyncio.timeout(60):
            return await build_traffic_route(body, config)
    except (httpx.TimeoutException, TimeoutError) as error:
        raise HTTPException(504, "Превышено время построения маршрута") from error
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(
            502, "Не удалось получить дорожный маршрут Valhalla"
        ) from error
