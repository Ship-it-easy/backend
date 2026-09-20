from typing import Any, Protocol


class JobImportOperations(Protocol):
    async def ensure_access(self, project_id: int, write: bool = False) -> None: ...
    async def upload(
        self, project_id: int, filename: str, content: bytes
    ) -> dict[str, Any]: ...
    async def get(self, project_id: int, batch_id: int) -> dict[str, Any]: ...
    async def list(
        self, project_id: int, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]: ...
    async def issues(
        self,
        project_id: int,
        batch_id: int,
        severity: str | None = None,
        query: str = "",
        page: int = 1,
    ) -> dict[str, Any]: ...
    async def revalidate(self, project_id: int, batch_id: int) -> dict[str, Any]: ...
    async def apply(
        self,
        project_id: int,
        batch_id: int,
        acknowledge_warnings: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...
