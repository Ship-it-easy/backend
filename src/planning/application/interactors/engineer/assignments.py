from typing import Any

from planning.application.access import ProjectAccess
from planning.application.errors import InvalidJobStatusError
from planning.application.interactors.job_status.change_job_status import (
    ChangeJobStatusInteractor,
)
from planning.application.interfaces.assignment_repository import AssignmentRepository


class _AssignmentInteractor:
    def __init__(self, access: ProjectAccess, repository: AssignmentRepository):
        self._access = access
        self._repository = repository


class ListAssignmentsInteractor(_AssignmentInteractor):
    async def __call__(self, scope: str) -> list[dict[str, Any]]:
        _, project_id, engineer_id = await self._access.engineer()
        return await self._repository.list_assignments(
            engineer_id,
            project_id,
            scope,
        )


class GetRouteStateInteractor(_AssignmentInteractor):
    async def __call__(self, scope: str) -> dict[str, Any]:
        _, project_id, engineer_id = await self._access.engineer()
        return await self._repository.get_route_state(
            engineer_id,
            project_id,
            scope,
        )


class GetAssignmentInteractor(_AssignmentInteractor):
    async def __call__(self, assignment_id: int) -> dict[str, Any]:
        _, _, engineer_id = await self._access.engineer()
        return await self._repository.get_assignment(engineer_id, assignment_id)


class _ChangeAssignmentInteractor(_AssignmentInteractor):
    target_status: str

    def __init__(
        self,
        access: ProjectAccess,
        repository: AssignmentRepository,
        change_status: ChangeJobStatusInteractor,
    ):
        super().__init__(access, repository)
        self._change_status = change_status

    async def __call__(self, assignment_id: int) -> dict[str, Any]:
        user, project_id, engineer_id = await self._access.engineer()
        context = await self._repository.get_change_context(
            engineer_id,
            project_id,
            assignment_id,
        )
        if (
            self.target_status == "IN_PROGRESS"
            and context["planning_date"] != context["today"]
        ):
            raise InvalidJobStatusError(
                "Only today's assignments can be changed by an engineer"
            )
        if self.target_status == "IN_PROGRESS" and not context["engineer_active"]:
            raise InvalidJobStatusError("Inactive engineer cannot start a new job")
        return await self._change_status(
            user,
            context["job_id"],
            self.target_status,
            project_id=project_id,
            engineer_id=engineer_id,
        )


class StartAssignmentInteractor(_ChangeAssignmentInteractor):
    target_status = "IN_PROGRESS"


class CompleteAssignmentInteractor(_ChangeAssignmentInteractor):
    target_status = "COMPLETED"


class ReturnAssignmentInteractor(_ChangeAssignmentInteractor):
    target_status = "NEW"
