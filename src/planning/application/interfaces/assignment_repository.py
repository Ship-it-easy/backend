from typing import Any, Protocol


class AssignmentRepository(Protocol):
    async def list_assignments(
        self, engineer_id: int, project_id: int, scope: str
    ) -> list[dict[str, Any]]: ...
    async def get_route_state(
        self, engineer_id: int, project_id: int, scope: str
    ) -> dict[str, Any]: ...
    async def get_assignment(
        self, engineer_id: int, assignment_id: int
    ) -> dict[str, Any]: ...
    async def get_change_context(
        self, engineer_id: int, project_id: int, assignment_id: int
    ) -> dict[str, Any]: ...
