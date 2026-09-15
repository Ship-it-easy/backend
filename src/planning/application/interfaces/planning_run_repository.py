from abc import abstractmethod
from datetime import date
from typing import Any, Protocol

from planning.domain.entities.planning import PlanningInput, PlanningResult


class PlanningRunRepository(Protocol):
    @abstractmethod
    async def get_project_timezone(self, project_id: int) -> str: ...

    @abstractmethod
    async def create_run(
        self,
        project_id: int,
        planning_date: date,
        timezone: str,
        initiated_by_user_id: Any | None = None,
    ) -> int: ...

    @abstractmethod
    async def load_source(
        self, project_id: int, planning_date: date
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def mark_running(self, run_id: int, data: PlanningInput) -> None: ...

    @abstractmethod
    async def save_result(
        self,
        run_id: int,
        data: PlanningInput,
        result: PlanningResult,
        *,
        commit: bool = True,
    ) -> None: ...

    @abstractmethod
    async def fail_run(self, run_id: int, code: str, message: str) -> None: ...

    @abstractmethod
    async def get_run(self, project_id: int, run_id: int) -> dict[str, Any]: ...

    @abstractmethod
    async def list_runs(
        self,
        project_id: int,
        planning_date: date | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]: ...
