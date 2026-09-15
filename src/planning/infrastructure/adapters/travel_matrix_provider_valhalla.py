import hashlib
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.errors import InvalidPlanningRequest, PlanningUnavailable
from planning.domain.entities.coordinate import Coordinate
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.persistence_sqla.mappings.tables import travel_time_cache


def _cache_key(origin: Coordinate, destination: Coordinate, profile: str) -> str:
    value = (
        f"{origin.latitude:.6f}:{origin.longitude:.6f}:"
        f"{destination.latitude:.6f}:{destination.longitude:.6f}:{profile}:osm"
    )
    return hashlib.sha256(value.encode()).hexdigest()


class ValhallaTravelMatrixProvider:
    def __init__(self, session: AsyncSession, config: PlanningServiceConfig):
        self._session = session
        self._config = config

    async def get_matrix(
        self, coordinates: list[Coordinate], profile: str
    ) -> list[list[int | None]]:
        size = len(coordinates)
        result: list[list[int | None]] = [[None] * size for _ in range(size)]
        missing: set[tuple[int, int]] = set()
        keys = {
            (i, j): _cache_key(origin, destination, profile)
            for i, origin in enumerate(coordinates)
            for j, destination in enumerate(coordinates)
        }
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=min(30, max(1, self._config.travel_cache_ttl_days))
        )
        cache_rows = (
            await self._session.execute(
                select(travel_time_cache).where(
                    travel_time_cache.c.cache_key.in_(list(keys.values()))
                )
            )
        ).mappings().all()
        stale_keys = [
            row.cache_key for row in cache_rows if row.created_at < cutoff
        ]
        cached = {
            row.cache_key: row.duration_min
            for row in cache_rows
            if row.created_at >= cutoff
        }
        if stale_keys:
            await self._session.execute(
                delete(travel_time_cache).where(
                    travel_time_cache.c.cache_key.in_(stale_keys)
                )
            )
        for pair, key in keys.items():
            if key in cached:
                result[pair[0]][pair[1]] = cached[key]
            else:
                missing.add(pair)

        block = max(1, self._config.matrix_block_size)
        async with httpx.AsyncClient(
            base_url=self._config.valhalla_url,
            timeout=self._config.geoservice_timeout_sec,
        ) as client:
            for source_start in range(0, size, block):
                source_ids = list(range(source_start, min(source_start + block, size)))
                for target_start in range(0, size, block):
                    target_ids = list(
                        range(target_start, min(target_start + block, size))
                    )
                    if not any(
                        (i, j) in missing for i in source_ids for j in target_ids
                    ):
                        continue
                    try:
                        response = await client.post(
                            "/sources_to_targets",
                            json={
                                "sources": [
                                    _location(coordinates[i]) for i in source_ids
                                ],
                                "targets": [
                                    _location(coordinates[j]) for j in target_ids
                                ],
                                "costing": profile,
                                "units": "kilometers",
                            },
                        )
                        response.raise_for_status()
                        matrix = response.json().get("sources_to_targets", [])
                    except httpx.HTTPStatusError as error:
                        status_code = error.response.status_code
                        if status_code in (429, 500, 502, 503, 504):
                            raise PlanningUnavailable(
                                "Travel provider temporarily unavailable",
                                code="SOLVER_OR_TRAVEL_PROVIDER_UNAVAILABLE",
                            ) from error
                        else:
                            # Client errors (400, 404, 422, etc.) indicate invalid request
                            raise InvalidPlanningRequest(
                                f"Travel provider returned {status_code}: invalid request"
                            ) from error
                    except (httpx.TimeoutException, httpx.RequestError, ValueError) as error:
                        # Network errors, timeouts, and JSON parsing errors
                        raise PlanningUnavailable(
                            "Travel provider is unavailable; retry the planning run",
                            code="SOLVER_OR_TRAVEL_PROVIDER_UNAVAILABLE",
                        ) from error
                    for local_i, row in enumerate(matrix):
                        for local_j, item in enumerate(row):
                            i, j = source_ids[local_i], target_ids[local_j]
                            if (i, j) not in missing:
                                continue
                            seconds = item.get("time") if item else None
                            result[i][j] = (
                                None
                                if seconds is None
                                else math.ceil(float(seconds) / 60)
                            )

        values = []
        for i, j in missing:
            origin, destination = coordinates[i], coordinates[j]
            values.append(
                {
                    "cache_key": keys[(i, j)],
                    "origin_latitude": Decimal(str(origin.latitude)),
                    "origin_longitude": Decimal(str(origin.longitude)),
                    "destination_latitude": Decimal(str(destination.latitude)),
                    "destination_longitude": Decimal(str(destination.longitude)),
                    "profile": profile,
                    "duration_min": result[i][j],
                    "provider": "VALHALLA_LOCAL",
                }
            )
        if values or stale_keys:
            if values:
                await self._session.execute(
                    insert(travel_time_cache)
                    .values(values)
                    .on_conflict_do_nothing(index_elements=["cache_key"])
                )
            await self._session.commit()
        return result


def _location(coordinate: Coordinate) -> dict[str, float]:
    return {"lat": coordinate.latitude, "lon": coordinate.longitude}

