import hashlib
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.interfaces.travel_matrix_provider import TravelMatrix
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
        self,
        coordinates: list[Coordinate],
        profile: str,
        cache_ttl_days: int | None = None,
    ) -> TravelMatrix:
        size = len(coordinates)
        times: list[list[int | None]] = [[None] * size for _ in range(size)]
        distances: list[list[int | None]] = [[None] * size for _ in range(size)]
        missing: set[tuple[int, int]] = set()
        keys = {
            (i, j): _cache_key(origin, destination, profile)
            for i, origin in enumerate(coordinates)
            for j, destination in enumerate(coordinates)
        }
        cache_query = select(travel_time_cache).where(
            travel_time_cache.c.cache_key.in_(list(keys.values()))
        )
        effective_ttl_days = 7 if cache_ttl_days is None else cache_ttl_days
        cache_query = cache_query.where(
            travel_time_cache.c.created_at
            >= datetime.now(timezone.utc) - timedelta(days=effective_ttl_days)
        )
        cached_rows = (await self._session.execute(cache_query)).mappings()
        cached = {row.cache_key: row for row in cached_rows}
        for pair, key in keys.items():
            row = cached.get(key)
            if (
                row is not None
                and row.travel_time_seconds is not None
                and row.distance_meters is not None
            ):
                times[pair[0]][pair[1]] = int(row.travel_time_seconds)
                distances[pair[0]][pair[1]] = int(row.distance_meters)
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
                            distance_km = item.get("distance") if item else None
                            if seconds is not None and distance_km is not None:
                                times[i][j] = math.ceil(float(seconds))
                                distances[i][j] = math.ceil(float(distance_km) * 1000)

            # Valhalla's matrix endpoint may omit otherwise routable long pairs
            # because of its matrix-distance limit. Resolve only those empty
            # cells through the same provider's route endpoint. This remains a
            # real road distance; if Valhalla cannot build the route, the cell
            # stays empty and the solver refuses publication.
            for i, j in sorted(missing):
                if times[i][j] is not None and distances[i][j] is not None:
                    continue
                if i == j:
                    times[i][j] = 0
                    distances[i][j] = 0
                    continue
                try:
                    response = await client.post(
                        "/route",
                        json={
                            "locations": [
                                _location(coordinates[i]),
                                _location(coordinates[j]),
                            ],
                            "costing": profile,
                            "units": "kilometers",
                        },
                    )
                    response.raise_for_status()
                    summary = response.json().get("trip", {}).get("summary", {})
                    seconds = summary.get("time")
                    distance_km = summary.get("length")
                    if seconds is not None and distance_km is not None:
                        times[i][j] = math.ceil(float(seconds))
                        distances[i][j] = math.ceil(float(distance_km) * 1000)
                except (httpx.HTTPError, TypeError, ValueError):
                    continue

        values_by_key = {}
        for i, j in missing:
            origin, destination = coordinates[i], coordinates[j]
            values_by_key[keys[(i, j)]] = {
                    "cache_key": keys[(i, j)],
                    "origin_latitude": Decimal(str(origin.latitude)),
                    "origin_longitude": Decimal(str(origin.longitude)),
                    "destination_latitude": Decimal(str(destination.latitude)),
                    "destination_longitude": Decimal(str(destination.longitude)),
                    "profile": profile,
                    "duration_min": (
                        math.ceil(times[i][j] / 60) if times[i][j] is not None else None
                    ),
                    "travel_time_seconds": times[i][j],
                    "distance_meters": distances[i][j],
                    "provider": "VALHALLA_LOCAL",
                }
        values = list(values_by_key.values())
        if values:
            await self._session.execute(
                insert(travel_time_cache)
                .values(values)
                .on_conflict_do_update(
                    index_elements=["cache_key"],
                    set_={
                        "duration_min": insert(travel_time_cache).excluded.duration_min,
                        "travel_time_seconds": insert(
                            travel_time_cache
                        ).excluded.travel_time_seconds,
                        "distance_meters": insert(
                            travel_time_cache
                        ).excluded.distance_meters,
                        "created_at": insert(travel_time_cache).excluded.created_at,
                    },
                )
            )
            await self._session.commit()
        return TravelMatrix(times, distances, profile, "VALHALLA_LOCAL")


def _location(coordinate: Coordinate) -> dict[str, float]:
    return {"lat": coordinate.latitude, "lon": coordinate.longitude}
