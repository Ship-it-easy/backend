from abc import abstractmethod
from dataclasses import dataclass
from typing import Protocol

from planning.domain.entities.coordinate import Coordinate


@dataclass(frozen=True)
class TravelMatrix:
    travel_time_seconds: list[list[int | None]]
    distance_meters: list[list[int | None]]
    profile: str
    source: str


class TravelMatrixProvider(Protocol):
    @abstractmethod
    async def get_matrix(
        self,
        coordinates: list[Coordinate],
        profile: str,
        cache_ttl_days: int | None = None,
    ) -> TravelMatrix: ...
