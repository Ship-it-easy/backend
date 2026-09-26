from typing import Any, Protocol


class BaselineRetryOperations(Protocol):
    async def retry(self, project_id: int, planning_run_id: int) -> dict[str, Any]: ...
