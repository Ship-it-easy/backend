import math

from planning.application.interfaces.travel_matrix_provider import TravelMatrix
from planning.domain.entities.coordinate import Coordinate


class StaticTravelMatrixProvider:
    """Deterministic offline provider used only by explicit STATIC_TEST config."""

    async def get_matrix(
        self,
        coordinates: list[Coordinate],
        profile: str,
        cache_ttl_days: int | None = None,
    ) -> TravelMatrix:
        speed_kmh = {"auto": 25, "pedestrian": 5, "bicycle": 15, "multimodal": 20}[
            profile
        ]
        times: list[list[int | None]] = []
        distances: list[list[int | None]] = []
        for origin in coordinates:
            time_row = []
            distance_row = []
            for destination in coordinates:
                distance_km = _haversine_km(origin, destination)
                distance_row.append(math.ceil(distance_km * 1000))
                time_row.append(math.ceil(distance_km / speed_kmh * 3600))
            times.append(time_row)
            distances.append(distance_row)
        return TravelMatrix(times, distances, profile, "STATIC_TEST")


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
