import hashlib
import math
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from auth.entrypoint.config import PlanningServiceConfig
from planning.domain.models import Coordinate
from planning.infrastructure.persistence.tables import travel_time_cache


def _cache_key(origin: Coordinate, destination: Coordinate, profile: str) -> str:
    value = (
        f"{origin.latitude:.6f}:{origin.longitude:.6f}:"
        f"{destination.latitude:.6f}:{destination.longitude:.6f}:{profile}:osm"
    )
    return hashlib.sha256(value.encode()).hexdigest()


class StaticTravelMatrixProvider:
    """Deterministic offline provider useful for local development and demo fallback."""

    async def get_matrix(
        self, coordinates: list[Coordinate], profile: str
    ) -> list[list[int | None]]:
        speed_kmh = 25 if profile == "auto" else 5
        result: list[list[int | None]] = []
        for origin in coordinates:
            row = []
            for destination in coordinates:
                distance_km = _haversine_km(origin, destination)
                row.append(math.ceil(distance_km / speed_kmh * 60))
            result.append(row)
        return result


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
        cached_rows = (
            await self._session.execute(
                select(travel_time_cache).where(
                    travel_time_cache.c.cache_key.in_(list(keys.values()))
                )
            )
        ).mappings()
        cached = {row.cache_key: row.duration_min for row in cached_rows}
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
                    response = await client.post(
                        "/sources_to_targets",
                        json={
                            "sources": [_location(coordinates[i]) for i in source_ids],
                            "targets": [_location(coordinates[j]) for j in target_ids],
                            "costing": profile,
                            "units": "kilometers",
                        },
                    )
                    response.raise_for_status()
                    matrix = response.json().get("sources_to_targets", [])
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


def _haversine_km(a: Coordinate, b: Coordinate) -> float:
    earth_radius = 6371.0
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(b.longitude - a.longitude)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * earth_radius * math.asin(math.sqrt(value))
