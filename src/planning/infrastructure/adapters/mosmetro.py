"""Small client and route helpers for the local MosMetro API wrapper."""

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx


@dataclass(frozen=True)
class MetroStation:
    id: int
    name: str
    line_id: int
    latitude: float
    longitude: float


@dataclass(frozen=True)
class MetroJourney:
    duration_seconds: int
    parts: list[dict]


MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")


def metro_operating_window(departure: datetime, duration_seconds: int = 0) -> bool:
    """Conservative fallback guard; the API route is not a dated timetable."""
    local = departure.astimezone(MOSCOW_TIMEZONE)
    minute = local.hour * 60 + local.minute
    service_minute = minute + (1440 if minute < 300 else 0)
    return 330 <= service_minute and service_minute + duration_seconds / 60 <= 1500


def distance_meters(first: tuple[float, float], second: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, first)
    lat2, lon2 = map(math.radians, second)
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 12_742_000 * math.asin(math.sqrt(value))


class MosMetroClient:
    """Access the repaired local wrapper and cache its station scheme."""

    def __init__(self, base_url: str, timeout: float = 15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._stations: list[MetroStation] | None = None
        self._station_by_id: dict[int, MetroStation] = {}
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    async def stations(self) -> list[MetroStation]:
        if self._stations is not None and time.monotonic() - self._loaded_at < 3600:
            return self._stations
        async with self._lock:
            if self._stations is not None and time.monotonic() - self._loaded_at < 3600:
                return self._stations
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=self.timeout
            ) as client:
                response = await client.get("/mosmetro/schema")
                response.raise_for_status()
                payload = response.json()
            rows = payload.get("stations", payload.get("data", {}).get("stations", []))
            stations = []
            for row in rows:
                location = row.get("location") or {}
                if row.get("perspective") or not location:
                    continue
                stations.append(
                    MetroStation(
                        id=int(row["id"]),
                        name=str((row.get("name") or {}).get("ru") or row["id"]),
                        line_id=int(row["lineId"]),
                        latitude=float(location["lat"]),
                        longitude=float(location["lon"]),
                    )
                )
            if not stations:
                raise ValueError("MosMetro returned no usable stations")
            self._stations = stations
            self._station_by_id = {station.id: station for station in stations}
            self._loaded_at = time.monotonic()
            return stations

    async def nearest(
        self, latitude: float, longitude: float, max_distance_meters: int
    ) -> MetroStation | None:
        stations = await self.stations()
        station = min(
            stations,
            key=lambda item: distance_meters(
                (latitude, longitude), (item.latitude, item.longitude)
            ),
        )
        distance = distance_meters(
            (latitude, longitude), (station.latitude, station.longitude)
        )
        return station if distance <= max_distance_meters else None

    async def route(self, origin_id: int, destination_id: int) -> MetroJourney | None:
        if origin_id == destination_id:
            return None
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout
        ) as client:
            response = await client.get(
                "/mosmetro/route",
                params={"from": origin_id, "to": destination_id},
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
        if not payload.get("success") or not payload.get("parts"):
            return None
        return MetroJourney(
            duration_seconds=math.ceil(float(payload["duration"])),
            parts=payload["parts"],
        )

    async def station(self, station_id: int) -> MetroStation | None:
        await self.stations()
        return self._station_by_id.get(station_id)


def approximate_access(
    point: tuple[float, float], station: MetroStation
) -> tuple[int, int]:
    """Estimate walking access for matrices; map routes use Valhalla geometry."""
    meters = math.ceil(
        distance_meters(point, (station.latitude, station.longitude)) * 1.25
    )
    return meters, math.ceil(meters / 1.3)


async def metro_matrix_candidate(
    client: MosMetroClient,
    origin: tuple[float, float],
    destination: tuple[float, float],
    max_access_meters: int,
    waiting_seconds: int,
    departure: datetime,
) -> tuple[int, int] | None:
    if not metro_operating_window(departure):
        return None
    origin_station, destination_station = await asyncio.gather(
        client.nearest(*origin, max_access_meters),
        client.nearest(*destination, max_access_meters),
    )
    if not origin_station or not destination_station:
        return None
    journey = await client.route(origin_station.id, destination_station.id)
    if not journey:
        return None
    origin_distance, origin_seconds = approximate_access(origin, origin_station)
    destination_distance, destination_seconds = approximate_access(
        destination, destination_station
    )
    total_seconds = (
        origin_seconds
        + waiting_seconds
        + journey.duration_seconds
        + destination_seconds
    )
    if not metro_operating_window(departure, total_seconds):
        return None
    rail_distance = 0
    for part in journey.parts:
        previous = None
        for node in part.get("nodes") or []:
            station = await client.station(int(node["id"]))
            if station and previous:
                rail_distance += distance_meters(
                    (previous.latitude, previous.longitude),
                    (station.latitude, station.longitude),
                )
            if station:
                previous = station
    return (
        total_seconds,
        math.ceil(origin_distance + rail_distance + destination_distance),
    )


async def pedestrian_leg(client, origin, destination, departure: datetime, evaluate):
    response = await client.post(
        "/route",
        json={
            "locations": [
                {"lat": origin[0], "lon": origin[1]},
                {"lat": destination[0], "lon": destination[1]},
            ],
            "costing": "pedestrian",
            "units": "kilometers",
            "shape_format": "polyline6",
            "date_time": {"type": 1, "value": departure.strftime("%Y-%m-%dT%H:%M")},
            "language": "ru-RU",
        },
    )
    response.raise_for_status()
    return evaluate(response.json()["trip"], departure, "pedestrian", False)


async def build_metro_map_candidate(
    metro: MosMetroClient,
    valhalla_client,
    origin: tuple[float, float],
    destination: tuple[float, float],
    departure: datetime,
    evaluate,
    max_access_meters: int,
    waiting_seconds: int,
) -> dict | None:
    if not metro_operating_window(departure):
        return None
    origin_station, destination_station = await asyncio.gather(
        metro.nearest(*origin, max_access_meters),
        metro.nearest(*destination, max_access_meters),
    )
    if not origin_station or not destination_station:
        return None
    journey = await metro.route(origin_station.id, destination_station.id)
    if not journey:
        return None
    first_walk = await pedestrian_leg(
        valhalla_client,
        origin,
        (origin_station.latitude, origin_station.longitude),
        departure,
        evaluate,
    )
    metro_departure = departure + timedelta(
        seconds=first_walk["duration_seconds"] + waiting_seconds
    )
    if not metro_operating_window(
        departure,
        first_walk["duration_seconds"] + waiting_seconds + journey.duration_seconds,
    ):
        return None
    metro_points: list[list[float]] = []
    metro_segments = []
    elapsed = 0
    part_total = sum(max(0, int(part.get("duration", 0))) for part in journey.parts)
    for index, part in enumerate(journey.parts):
        nodes = part.get("nodes") or []
        points = []
        for node in nodes:
            station = await metro.station(int(node["id"]))
            if station:
                points.append([station.latitude, station.longitude])
        if len(points) < 2:
            continue
        seconds = max(0, int(part.get("duration", 0)))
        if index == 0:
            seconds += waiting_seconds
        entered = departure + timedelta(
            seconds=first_walk["duration_seconds"] + elapsed
        )
        line_name = str(nodes[0].get("lineName") or "Метро")
        metro_segments.append(
            {
                "road": line_name,
                "travel_mode": "transit",
                "travel_type": "metro",
                "transit": {
                    "route": line_name,
                    "from_stop": nodes[0].get("name"),
                    "to_stop": nodes[-1].get("name"),
                },
                "corridor_id": None,
                "traffic_source": "mosmetro",
                "direction": "both",
                "points": points,
                "departure_at": entered.isoformat(),
                "baseline_seconds": seconds,
                "duration_seconds": seconds,
                "coefficient": 1,
                "quality": "mosmetro_route",
            }
        )
        elapsed += seconds
        metro_points.extend(points if not metro_points else points[1:])
    if not metro_segments:
        return None
    # Prefer API total when part durations are absent or differ.
    metro_seconds = journey.duration_seconds + waiting_seconds
    if part_total:
        correction = metro_seconds - sum(
            segment["duration_seconds"] for segment in metro_segments
        )
        metro_segments[-1]["duration_seconds"] += correction
        metro_segments[-1]["baseline_seconds"] += correction
    final_departure = metro_departure + timedelta(seconds=journey.duration_seconds)
    final_walk = await pedestrian_leg(
        valhalla_client,
        (destination_station.latitude, destination_station.longitude),
        destination,
        final_departure,
        evaluate,
    )
    points = first_walk["points"] + metro_points[1:] + final_walk["points"][1:]
    segments = first_walk["segments"] + metro_segments + final_walk["segments"]
    seconds = sum(segment["duration_seconds"] for segment in segments)
    return {
        "points": points,
        "segments": segments,
        "duration_seconds": seconds,
        "baseline_seconds": seconds,
        "uncovered_baseline_seconds": 0,
        "district_fallback_baseline_seconds": 0,
        "coefficient": 1,
        "provider": "MOSMETRO_HYBRID",
        "metro_origin": origin_station.name,
        "metro_destination": destination_station.name,
    }
