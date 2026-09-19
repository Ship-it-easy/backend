from abc import abstractmethod
from typing import Any, Protocol


class JobsRepository(Protocol):
    @abstractmethod
    async def create_job(
        self, project_id: int, values: dict[str, Any], actor_user_id: Any
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def list_jobs(self, project_id: int) -> list[dict[str, Any]]: ...
