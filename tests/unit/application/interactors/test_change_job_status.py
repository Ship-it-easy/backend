from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from planning.application.errors import InvalidJobStatusError, ObjectNotFoundError
from planning.application.interactors.job_status.change_job_status import (
    ChangeJobStatusInteractor,
)
from planning.application.interfaces.job_status_repository import JobStatusContext


def _context(**changes) -> JobStatusContext:
    values = {
        "job_id": 10,
        "old_status": "NEW",
        "assignment_id": 20,
        "assignment_engineer_id": 30,
        "assignment_matches_engineer": True,
        "another_job_in_progress": False,
        "previous_job_unfinished": False,
        "later_job_started": False,
    }
    values.update(changes)
    return JobStatusContext(**values)


async def test_dispatcher_can_cancel_new_job() -> None:
    repository = AsyncMock()
    repository.load_transition_context.return_value = _context()
    repository.save_transition.return_value = {
        "job_id": 10,
        "old_status": "NEW",
        "status": "CANCELLED",
    }
    user = Mock(id=uuid4())
    interactor = ChangeJobStatusInteractor(repository, AsyncMock())

    result = await interactor(
        user,
        10,
        "CANCELLED",
        project_id=1,
        dispatcher=True,
    )

    assert result["status"] == "CANCELLED"
    repository.save_transition.assert_awaited_once()


async def test_engineer_cannot_cancel_job() -> None:
    repository = AsyncMock()
    repository.load_transition_context.return_value = _context()
    interactor = ChangeJobStatusInteractor(repository, AsyncMock())

    with pytest.raises(InvalidJobStatusError, match="Engineer cannot"):
        await interactor(Mock(id=uuid4()), 10, "CANCELLED", project_id=1)


async def test_start_requires_published_assignment() -> None:
    repository = AsyncMock()
    repository.load_transition_context.return_value = _context(assignment_id=None)
    interactor = ChangeJobStatusInteractor(repository, AsyncMock())

    with pytest.raises(InvalidJobStatusError, match="published assignment"):
        await interactor(Mock(id=uuid4()), 10, "IN_PROGRESS", project_id=1)


async def test_completed_job_requires_reason_to_reopen() -> None:
    repository = AsyncMock()
    repository.load_transition_context.return_value = _context(
        old_status="COMPLETED"
    )
    interactor = ChangeJobStatusInteractor(repository, AsyncMock())

    with pytest.raises(InvalidJobStatusError, match="Reason is required"):
        await interactor(
            Mock(id=uuid4()),
            10,
            "IN_PROGRESS",
            project_id=1,
            dispatcher=True,
        )


async def test_engineer_cannot_change_another_assignment() -> None:
    repository = AsyncMock()
    repository.load_transition_context.return_value = _context(
        assignment_matches_engineer=False
    )
    interactor = ChangeJobStatusInteractor(repository, AsyncMock())

    with pytest.raises(ObjectNotFoundError, match="Assignment not found"):
        await interactor(
            Mock(id=uuid4()),
            10,
            "IN_PROGRESS",
            project_id=1,
            engineer_id=99,
        )
