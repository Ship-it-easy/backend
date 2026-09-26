import asyncio
from datetime import date
from typing import Literal

import httpx
from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, HTTPException
from pydantic import AwareDatetime, BaseModel, Field

from planning.application.access import ProjectAccess
from planning.application.interfaces.traffic_route_service import TrafficRouteService
from planning.domain.traffic import (
    INTERVAL_MINUTES,
    MOSCOW,
    QUALITY,
    VERSION,
    profile_table,
)
from planning.entrypoint.config import PlanningServiceConfig

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
    profile: Literal["auto", "pedestrian", "bicycle", "multimodal"] = "auto"
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
        "coverage_bounds": None,
        "areas": ["Москва", "Домодедово", "Ступино", "Кашира", "Прочие районы"],
        "coverage_policy": "district_factor_for_all_automobile_segments",
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
    routes: FromDishka[TrafficRouteService],
) -> dict:
    await access.dispatcher()
    return await build_traffic_route_response(body, routes)


async def build_traffic_route_response(
    body: TrafficRouteRequest, routes: TrafficRouteService
) -> dict:
    # Valhalla interprets date_time as local to the origin, not as UTC.
    body.departure_at = body.departure_at.astimezone(MOSCOW)
    try:
        # One road request is needed for each consecutive pair of stops.  Keep
        # a bounded per-leg budget so a long published route is not cut off by
        # the former fixed one-minute limit.
        timeout_seconds = min(300, 15 + 3 * (len(body.stops) - 1))
        async with asyncio.timeout(timeout_seconds):
            return await routes.build(body)
    except (httpx.TimeoutException, TimeoutError) as error:
        raise HTTPException(504, "Превышено время построения маршрута") from error
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(
            502, "Не удалось получить дорожный маршрут Valhalla"
        ) from error
