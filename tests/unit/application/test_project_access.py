from unittest.mock import AsyncMock, Mock

import pytest

from auth.domain.user_role import UserRoleEnum
from planning.application.access import ProjectAccess
from planning.application.errors import (
    AccessDeniedError,
    ObjectNotFoundError,
    ProjectBlockedError,
)
from planning.application.interactors.dynamic_planning import (
    GetPlanningBoardInteractor,
)
from planning.application.interactors.project.planning import (
    GetPlanningConfigInteractor,
)


def _user(
    role: UserRoleEnum,
    *,
    project_id: int | None = None,
    engineer_id: int | None = None,
) -> Mock:
    return Mock(role=role, project_id=project_id, engineer_id=engineer_id)


async def test_owner_rejects_dispatcher() -> None:
    identity = AsyncMock()
    identity.get_current_user.return_value = _user(
        UserRoleEnum.DISPATCHER, project_id=1
    )
    access = ProjectAccess(identity, AsyncMock())

    with pytest.raises(AccessDeniedError, match="Owner role is required"):
        await access.owner()


async def test_dispatcher_requires_active_project() -> None:
    identity = AsyncMock()
    identity.get_current_user.return_value = _user(
        UserRoleEnum.DISPATCHER, project_id=7
    )
    repository = AsyncMock()
    repository.get_project_status.return_value = "BLOCKED"
    access = ProjectAccess(identity, repository)

    with pytest.raises(ProjectBlockedError, match="Project is blocked"):
        await access.dispatcher()


async def test_project_hides_other_tenant() -> None:
    identity = AsyncMock()
    identity.get_current_user.return_value = _user(
        UserRoleEnum.DISPATCHER, project_id=7
    )
    access = ProjectAccess(identity, AsyncMock())

    with pytest.raises(ObjectNotFoundError, match="Object not found"):
        await access.project(8)


async def test_engineer_returns_identity_scope() -> None:
    user = _user(UserRoleEnum.ENGINEER, project_id=7, engineer_id=11)
    identity = AsyncMock()
    identity.get_current_user.return_value = user
    repository = AsyncMock()
    repository.get_project_status.return_value = "ACTIVE"
    access = ProjectAccess(identity, repository)

    assert await access.engineer() == (user, 7, 11)


async def test_engineer_cannot_open_planning_board() -> None:
    identity = AsyncMock()
    identity.get_current_user.return_value = _user(
        UserRoleEnum.ENGINEER, project_id=7, engineer_id=11
    )
    access = ProjectAccess(identity, AsyncMock())
    interactor = GetPlanningBoardInteractor(
        access,
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    )

    with pytest.raises(AccessDeniedError):
        await interactor(from_date=None, days=7)


async def test_owner_can_read_scoped_planning_config() -> None:
    identity = AsyncMock()
    identity.get_current_user.return_value = _user(UserRoleEnum.OWNER)
    management_repository = AsyncMock()
    management_repository.get_config.return_value = {"version": 4}
    interactor = GetPlanningConfigInteractor(
        ProjectAccess(identity, AsyncMock()), management_repository
    )

    assert await interactor(7) == {"version": 4}
    management_repository.get_config.assert_awaited_once_with(7)


async def test_owner_project_scope_must_exist_and_be_active_for_write() -> None:
    identity = AsyncMock()
    identity.get_current_user.return_value = _user(UserRoleEnum.OWNER)
    repository = AsyncMock()
    access = ProjectAccess(identity, repository)

    repository.get_project_status.return_value = None
    with pytest.raises(ObjectNotFoundError):
        await access.project(404)

    repository.get_project_status.return_value = "BLOCKED"
    assert await access.project(7) is identity.get_current_user.return_value
    with pytest.raises(ProjectBlockedError):
        await access.project(7, write=True)
