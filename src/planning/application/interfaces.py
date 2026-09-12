from abc import abstractmethod
from datetime import date
from typing import Any, Protocol

from planning.domain.models import Coordinate, PlanningInput, PlanningResult


class Geocoder(Protocol):
    @abstractmethod
    async def geocode(self, address: str) -> Coordinate | None: ...


class TravelMatrixProvider(Protocol):
    @abstractmethod
    async def get_matrix(
        self, coordinates: list[Coordinate], profile: str
    ) -> list[list[int | None]]: ...


class PlanningRunRepository(Protocol):
    @abstractmethod
    async def get_project_timezone(self, project_id: int) -> str: ...

    @abstractmethod
    async def create_run(
        self, project_id: int, planning_date: date, timezone: str, initiated_by_user_id: Any | None = None
    ) -> int: ...

    @abstractmethod
    async def load_source(
        self, project_id: int, planning_date: date
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def mark_running(self, run_id: int, data: PlanningInput) -> None: ...

    @abstractmethod
    async def save_result(
        self, run_id: int, data: PlanningInput, result: PlanningResult
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


class JobsRepository(Protocol):
    @abstractmethod
    async def create_project(self, values: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    async def list_projects(self) -> list[dict[str, Any]]: ...
    @abstractmethod
    async def create_job(
        self, project_id: int, values: dict[str, Any]
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def list_jobs(self, project_id: int) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def create_jobs(
        self, project_id: int, values: list[dict[str, Any]]
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def create_engineer(self, project_id: int, values: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    async def list_engineers(self, project_id: int) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def create_schedule(self, engineer_id: int, values: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    async def list_schedules(self, engineer_id: int) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def set_equipment_availability(self, project_id: int, values: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    async def list_equipment_availability(self, project_id: int, availability_date: date | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def create_catalog_item(self, table: Any, project_id: int, values: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    async def list_catalog_items(self, table: Any, project_id: int) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def replace_engineer_qualifications(self, engineer_id: int, qualification_ids: list[int]) -> None: ...

    @abstractmethod
    async def replace_work_type_requirements(self, work_type_id: int, qualification_ids: list[int], equipment_type_ids: list[int]) -> None: ...
