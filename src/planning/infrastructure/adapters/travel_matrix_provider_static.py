import math

from planning.domain.entities.coordinate import Coordinate


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
