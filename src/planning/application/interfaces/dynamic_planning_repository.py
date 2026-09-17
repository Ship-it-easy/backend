from typing import Any, Protocol


class DynamicPlanningRepository(Protocol):
    async def enqueue(
        self,
        project_id: int,
        event_type: str,
        actor_user_id: Any,
        idempotency_key: str,
        job_ids: list[int] | None = None,
    ) -> dict[str, Any]: ...

    async def get_event(
        self, project_id: int, event_id: int
    ) -> dict[str, Any] | None: ...

    async def get_current_plan(self, project_id: int) -> dict[str, Any] | None: ...

    async def list_plan_versions(
        self, project_id: int, limit: int, offset: int
    ) -> dict[str, Any]: ...

    async def get_plan_version(
        self, project_id: int, version_id: int
    ) -> dict[str, Any] | None: ...
