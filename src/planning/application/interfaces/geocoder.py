from abc import abstractmethod
from typing import Protocol

from planning.domain.entities.coordinate import Coordinate


class Geocoder(Protocol):
    @abstractmethod
    async def geocode(self, address: str) -> Coordinate | None: ...
