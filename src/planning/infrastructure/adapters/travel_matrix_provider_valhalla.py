import asyncio
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
from planning.infrastructure.adapters.mosmetro import (
    MosMetroClient,
    metro_matrix_candidate,
)
from planning.infrastructure.persistence_sqla.mappings.tables import travel_time_cache


def _cache_key(origin: Coordinate, destination: Coordinate, profile: str) -> str:
    value = (
        f"{origin.latitude:.6f}:{origin.longitude:.6f}:"
        f"{destination.latitude:.6f}:{destination.longitude:.6f}:{profile}:osm-freeflow-v2"
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _deduplicate_coordinates(
    coordinates: list[Coordinate],
) -> tuple[list[Coordinate], list[int]]:
    """Return unique points and a map from each input position to its point.

    Coordinates are rounded in the cache key to six decimals, so use the same
    precision here. This keeps duplicate geocoding results from producing
    duplicate Valhalla calls while preserving the original matrix shape.
    """
    unique: list[Coordinate] = []
    by_key: dict[tuple[float, float], int] = {}
    positions: list[int] = []
    for coordinate in coordinates:
        key = (round(coordinate.latitude, 6), round(coordinate.longitude, 6))
        index = by_key.get(key)
        if index is None:
            index = len(unique)
            by_key[key] = index
            unique.append(coordinate)
        positions.append(index)
    return unique, positions


def _expand_matrix(
    values: list[list[int | None]], positions: list[int]
) -> list[list[int | None]]:
    return [[values[source][target] for target in positions] for source in positions]


def _limit_missing_pairs(
    missing: set[tuple[int, int]],
    coordinates: list[Coordinate],
    limit: int | None,
) -> set[tuple[int, int]]:
    """Keep only the nearest unresolved destinations for each origin.

    Cached values are deliberately not removed: the limit applies only to new
    Valhalla calls. A missing pair outside the shortlist remains unavailable
    to the solver, so the returned matrix shape is unchanged.
    """
    if limit is None or limit <= 0:
        return missing
    result: set[tuple[int, int]] = set()
    for source in range(len(coordinates)):
        targets = [target for origin, target in missing if origin == source]
        targets.sort(
            key=lambda target: (
                (coordinates[source].latitude - coordinates[target].latitude) ** 2
                + (coordinates[source].longitude - coordinates[target].longitude) ** 2
            )
        )
        result.update((source, target) for target in targets[:limit])
    return result


class ValhallaTravelMatrixProvider:
    def __init__(self, session: AsyncSession, config: PlanningServiceConfig):
        self._session = session
        self._config = config
        self.planning_traffic_enabled = config.traffic_model_enabled
        self._metro = MosMetroClient(
            getattr(config, "mosmetro_url", ""),
            config.geoservice_timeout_sec,
        )

    async def get_matrix_for_departure(
        self, coordinates: list[Coordinate], profile: str, departure_at: datetime
    ) -> TravelMatrix:
        """Transit needs dated route pairs: Valhalla has no transit matrix API.

        Event snapshots own reuse; never put timetable results in the road cache.
        Planning uses shift start as an approximation, map legs use actual times.
        """
        if profile != "multimodal":
            raise ValueError("Departure-specific pairs are for public transport")
        coordinates, positions = _deduplicate_coordinates(coordinates)
        size = len(coordinates)
        # The solver must see only real multimodal values. A walking value in
        # this matrix would make a public-transport engineer appear able to
        # travel faster than the route shown later on the map.
        times: list[list[int | None]] = [[None] * size for _ in range(size)]
        distances: list[list[int | None]] = [[None] * size for _ in range(size)]
        async with httpx.AsyncClient(
            base_url=self._config.valhalla_url,
            timeout=max(60, self._config.geoservice_timeout_sec),
        ) as client:

            async def fetch(i: int, j: int) -> None:
                origin, destination = coordinates[i], coordinates[j]
                if origin == destination:
                    times[i][j] = distances[i][j] = 0
                    return
                response = await client.post(
                    "/route",
                    json={
                        "locations": [_location(origin), _location(destination)],
                        "costing": profile,
                        "date_time": {
                            "type": 1,
                            "value": departure_at.astimezone(
                                timezone(timedelta(hours=3))
                            ).strftime("%Y-%m-%dT%H:%M"),
                        },
                        "units": "kilometers",
                    },
                )
                valhalla_unreachable = (
                    response.status_code == 400
                    and response.json().get("error_code") in {441, 442}
                )
                if not valhalla_unreachable:
                    response.raise_for_status()
                    trip = response.json()["trip"]
                    summary = trip["summary"]
                    times[i][j] = math.ceil(float(summary["time"]))
                    distances[i][j] = math.ceil(float(summary["length"]) * 1000)
                has_gtfs_transit = not valhalla_unreachable and any(
                    maneuver.get("travel_mode") == "transit"
                    for leg in trip.get("legs", [])
                    for maneuver in leg.get("maneuvers", [])
                )
                if self._metro.enabled and not has_gtfs_transit:
                    try:
                        metro = await metro_matrix_candidate(
                            self._metro,
                            (origin.latitude, origin.longitude),
                            (destination.latitude, destination.longitude),
                            self._config.mosmetro_max_access_meters,
                            self._config.mosmetro_waiting_seconds,
                            departure_at,
                        )
                    except (httpx.HTTPError, ValueError, KeyError, TypeError):
                        metro = None
                    if metro and (times[i][j] is None or metro[0] < times[i][j]):
                        times[i][j], distances[i][j] = metro

            # Keep at most eight requests/tasks alive. Creating a task for every
            # matrix cell exhausts memory when the planning batch is large.
            batch: list[asyncio.Task[None]] = []
            for i in range(size):
                for j in range(size):
                    batch.append(asyncio.create_task(fetch(i, j)))
                    if len(batch) == 8:
                        await asyncio.gather(*batch)
                        batch.clear()
            if batch:
                await asyncio.gather(*batch)
        source = (
            "VALHALLA_TRANSIT+MOSMETRO" if self._metro.enabled else "VALHALLA_TRANSIT"
        )
        return TravelMatrix(
            _expand_matrix(times, positions),
            _expand_matrix(distances, positions),
            profile,
            source,
        )

    async def get_matrix(
        self,
        coordinates: list[Coordinate],
        profile: str,
        cache_ttl_days: int | None = None,
    ) -> TravelMatrix:
        if profile == "multimodal":
            raise ValueError("Public transport requires a planning departure date")
        coordinates, positions = _deduplicate_coordinates(coordinates)
        size = len(coordinates)
        times: list[list[int | None]] = [[None] * size for _ in range(size)]
        distances: list[list[int | None]] = [[None] * size for _ in range(size)]
        missing: set[tuple[int, int]] = set()
        keys = {
            (i, j): _cache_key(origin, destination, profile)
            for i, origin in enumerate(coordinates)
            for j, destination in enumerate(coordinates)
        }
        effective_ttl_days = 7 if cache_ttl_days is None else cache_ttl_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=effective_ttl_days)
        unique_keys = sorted(set(keys.values()))
        cached = {}
        # PostgreSQL's bind limit applies even though Valhalla requests are blocked.
        for offset in range(0, len(unique_keys), 5_000):
            cache_query = select(travel_time_cache).where(
                travel_time_cache.c.cache_key.in_(unique_keys[offset : offset + 5_000]),
                travel_time_cache.c.created_at >= cutoff,
            )
            cached_rows = (await self._session.execute(cache_query)).mappings()
            cached.update({row.cache_key: row for row in cached_rows})
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
        missing = _limit_missing_pairs(
            missing, coordinates, self._config.matrix_candidate_limit
        )

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
                                "costing_options": {
                                    profile: {"speed_types": ["freeflow"]}
                                },
                                "units": "kilometers",
                            },
                        )
                        response.raise_for_status()
                    except httpx.HTTPStatusError as error:
                        # The matrix service rejects a whole block if even one pair
                        # exceeds its configured matrix-distance limit. Individual
                        # routes use a larger limit and are resolved below.
                        if error.response.status_code != 400:
                            raise
                        continue
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
            # stays empty and that arc is forbidden in the routing model.
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
                            "costing_options": {profile: {"speed_types": ["freeflow"]}},
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
                except httpx.HTTPStatusError as error:
                    # Only an explicit no-route response means an unreachable arc.
                    # A server/network failure must not silently drop customer jobs.
                    if error.response.status_code == 400 and error.response.json().get(
                        "error_code"
                    ) in {441, 442}:
                        continue
                    raise

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
        for offset in range(0, len(values), 1_000):
            await self._session.execute(
                insert(travel_time_cache)
                .values(values[offset : offset + 1_000])
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
        if values:
            await self._session.commit()
        return TravelMatrix(
            _expand_matrix(times, positions),
            _expand_matrix(distances, positions),
            profile,
            "VALHALLA_LOCAL",
        )


def _location(coordinate: Coordinate) -> dict[str, float]:
    return {"lat": coordinate.latitude, "lon": coordinate.longitude}
