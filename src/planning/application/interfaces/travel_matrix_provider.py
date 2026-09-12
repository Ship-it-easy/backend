from abc import abstractmethod
from typing import Protocol

from planning.domain.entities.coordinate import Coordinate


class TravelMatrixProvider(Protocol):
    @abstractmethod
    async def get_matrix(
        self, coordinates: list[Coordinate], profile: str
    ) -> list[list[int | None]]: ...
