from datetime import date, datetime, time, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from auth.domain.user_role import UserRoleEnum
from planning.application.errors import (
    ConflictError,
    InvalidJobStatusError,
    InvalidPlanningRequest,
    ObjectNotFoundError,
)
from planning.application.interactors.admin.users import (
    BlockUserInteractor,
    CreateProjectUserInteractor,
)
from planning.application.interactors.project.engineer_access import (
    CreateEngineerAccessInteractor,
)
from planning.application.interactors.project.jobs import UpdateProjectJobInteractor
from planning.application.interactors.project.planning import (
    PublishPlanningRunInteractor,
)
from planning.application.management_dto import (
    PlanningPublicationState,
    PlanningRouteAssignment,
    ProjectJobEditState,
    ProjectUserValidationState,
    PublishedPlan,
    UserActivationState,
    WorkTypeEditState,
)

USER_ID = UUID("00000000-0000-0000-0000-000000000001")


class FakeUow:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self.rolled_back = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def access() -> AsyncMock:
    value = AsyncMock()
    value.owner.return_value = MagicMock(id=USER_ID)
    value.dispatcher.return_value = (MagicMock(id=USER_ID), 10)
    return value


def publication_state(**changes) -> PlanningPublicationState:
    route = PlanningRouteAssignment(
        job_id=7,
        engineer_id=8,
        sequence=1,
        planned_arrival=datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
        planned_start=datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
        planned_finish=datetime(2026, 1, 1, 9, tzinfo=timezone.utc),
        travel_from_previous_min=0,
        waiting_before_job_min=0,
    )
    values = {
        "run_id": 1,
        "planning_date": date(2026, 1, 1),
        "status": "SUCCESS",
        "validation_errors": (),
        "unassigned_jobs_count": 0,
        "already_published": False,
        "daily_plan_id": None,
        "current_version_id": None,
        "current_plan_started": False,
        "max_version_number": 0,
        "routes": (route,),
        "publishable_job_ids": frozenset({7}),
        "snapshots": {},
    }
    values.update(changes)
    return PlanningPublicationState(**values)


@pytest.mark.parametrize(
    ("changes", "confirm", "code"),
    [
        ({"status": "FAILED"}, True, "PLANNING_RUN_NOT_PUBLISHABLE"),
        ({"unassigned_jobs_count": 1}, False, "UNASSIGNED_CONFIRMATION_REQUIRED"),
        ({"already_published": True}, True, "ALREADY_PUBLISHED"),
        ({"current_plan_started": True}, True, "PLAN_ALREADY_STARTED"),
    ],
)
async def test_publish_rejects_invalid_state(changes, confirm, code) -> None:
    repository = AsyncMock()
    repository.load_publication_state.return_value = publication_state(**changes)
    interactor = PublishPlanningRunInteractor(access(), repository, FakeUow())

    with pytest.raises(ConflictError) as raised:
        await interactor(1, confirm)

    assert raised.value.code == code
    repository.save_publication.assert_not_awaited()


async def test_publish_success_builds_next_version_command() -> None:
    repository = AsyncMock()
    repository.load_publication_state.return_value = publication_state(
        daily_plan_id=3, current_version_id=4, max_version_number=5
    )
    repository.save_publication.return_value = PublishedPlan(3, 6, 6, 1)
    uow = FakeUow()

    result = await PublishPlanningRunInteractor(access(), repository, uow)(1, True)

    command = repository.save_publication.await_args.args[0]
    assert command.version_number == 6
    assert command.previous_version_id == 4
    assert result["assignments_count"] == 1
    assert uow.committed


def job_state(**changes) -> ProjectJobEditState:
    values = {
        "id": 1,
        "project_id": 10,
        "status": "NEW",
        "published": False,
        "work_type_id": 2,
        "service_duration_min": 30,
        "address": "Old",
        "latitude": None,
        "longitude": None,
        "time_window_start": time(8),
        "time_window_end": time(10),
    }
    values.update(changes)
    return ProjectJobEditState(**values)


def job_interactor(state: ProjectJobEditState):
    repository = AsyncMock()
    repository.load_job_for_update.return_value = state
    repository.get_work_type_for_edit.return_value = WorkTypeEditState(2, 10, True, 30)
    repository.save_job.return_value = {"id": 1}
    return UpdateProjectJobInteractor(access(), repository, FakeUow()), repository


async def test_update_job_rejects_non_new() -> None:
    interactor, _ = job_interactor(job_state(status="IN_PROGRESS"))
    with pytest.raises(InvalidJobStatusError):
        await interactor(1, {})


async def test_update_job_rejects_published() -> None:
    interactor, _ = job_interactor(job_state(published=True))
    with pytest.raises(ConflictError) as raised:
        await interactor(1, {})
    assert raised.value.code == "JOB_PUBLISHED"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"latitude": 1.0}, "INVALID_COORDINATES"),
        (
            {"time_window_start": time(23), "time_window_end": time(1)},
            "INVALID_TIME_WINDOW",
        ),
    ],
)
async def test_update_job_validates_patch(changes, code) -> None:
    interactor, _ = job_interactor(job_state())
    with pytest.raises(InvalidPlanningRequest) as raised:
        await interactor(1, changes)
    assert raised.value.code == code


@pytest.mark.parametrize(
    "work_type",
    [None, WorkTypeEditState(2, 99, True, 30), WorkTypeEditState(2, 10, False, 30)],
)
async def test_update_job_rejects_inactive_or_cross_project_work_type(
    work_type,
) -> None:
    interactor, repository = job_interactor(job_state())
    repository.get_work_type_for_edit.return_value = work_type
    with pytest.raises(InvalidPlanningRequest) as raised:
        await interactor(1, {})
    assert raised.value.code == "CROSS_PROJECT_REFERENCE"


async def test_update_address_clears_coordinates_and_geocoding() -> None:
    interactor, repository = job_interactor(job_state(latitude=1.0, longitude=2.0))
    await interactor(1, {"address": "New"})
    changes = repository.save_job.await_args.args[1]
    assert changes["latitude"] is None
    assert changes["longitude"] is None
    assert changes["address_hash"] is None
    assert changes["geocoded_at"] is None


async def test_block_last_active_owner() -> None:
    repository = AsyncMock()
    repository.lock_activation.return_value = UserActivationState(
        USER_ID, "owner", UserRoleEnum.OWNER, None, None, True, 1
    )
    interactor = BlockUserInteractor(access(), repository, MagicMock(), FakeUow())
    with pytest.raises(ConflictError) as raised:
        await interactor(USER_ID)
    assert raised.value.code == "LAST_ACTIVE_OWNER"
    repository.save_active.assert_not_awaited()


async def test_block_inactive_owner_is_idempotent() -> None:
    repository = AsyncMock()
    repository.lock_activation.return_value = UserActivationState(
        USER_ID, "owner", UserRoleEnum.OWNER, None, None, False, 1
    )
    result = await BlockUserInteractor(access(), repository, MagicMock(), FakeUow())(
        USER_ID
    )
    assert result["status"] == "BLOCKED"
    repository.save_active.assert_not_awaited()


async def test_create_project_user_rejects_invalid_role() -> None:
    interactor = CreateProjectUserInteractor(
        access(), AsyncMock(), MagicMock(), FakeUow()
    )
    with pytest.raises(InvalidPlanningRequest) as raised:
        await interactor(10, "login", "password", UserRoleEnum.OWNER, None)
    assert raised.value.code == "INVALID_ROLE"


async def test_create_project_user_rejects_cross_project_engineer() -> None:
    repository = AsyncMock()
    repository.get_project_user_validation.return_value = ProjectUserValidationState(
        True, True, False
    )
    interactor = CreateProjectUserInteractor(
        access(), repository, MagicMock(), FakeUow()
    )
    with pytest.raises(InvalidPlanningRequest) as raised:
        await interactor(10, "login", "password", UserRoleEnum.ENGINEER, 4)
    assert raised.value.code == "CROSS_PROJECT_REFERENCE"


async def test_engineer_access_rejects_engineer_from_another_project() -> None:
    repository = AsyncMock()
    repository.engineer_belongs_to_project.return_value = False
    interactor = CreateEngineerAccessInteractor(
        access(), repository, MagicMock(), FakeUow()
    )
    with pytest.raises(ObjectNotFoundError):
        await interactor(4, "login", "password")
