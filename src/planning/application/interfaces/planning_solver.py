from abc import abstractmethod
from typing import Protocol

from planning.domain.entities.planning import PlanningInput, PlanningResult


class PlanningSolver(Protocol):
    @abstractmethod
    async def solve(self, data: PlanningInput) -> PlanningResult: ...


class PlanningSolverFactory(Protocol):
    @abstractmethod
    def create(self, provider: str) -> PlanningSolver: ...
