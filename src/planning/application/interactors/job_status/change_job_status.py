from typing import Any

from auth.domain.entities.user import User
from planning.application.errors import InvalidJobStatusError, ObjectNotFoundError
from planning.application.interfaces.job_status_repository import (
    JobStatusRepository,
)


class ChangeJobStatusInteractor:
    def __init__(self, repository: JobStatusRepository):
        self._repository = repository

    async def __call__(
        self,
        user: User,
        job_id: int,
        new_status: str,
        *,
        project_id: int,
        engineer_id: int | None = None,
        reason: str | None = None,
        dispatcher: bool = False,
    ) -> dict[str, Any]:
        context = await self._repository.load_transition_context(
            job_id,
            project_id,
            engineer_id,
            new_status,
        )
        if not context.assignment_matches_engineer:
            raise ObjectNotFoundError("Assignment not found")

        allowed = {
            "NEW": {"IN_PROGRESS", "CANCELLED"},
            "IN_PROGRESS": {"COMPLETED", "NEW", "CANCELLED"},
            "COMPLETED": {"IN_PROGRESS"},
            "CANCELLED": {"NEW"},
        }
        if new_status not in allowed.get(context.old_status, set()):
            self._invalid("Requested status transition is not allowed")
        if not dispatcher and (context.old_status, new_status) not in {
            ("NEW", "IN_PROGRESS"),
            ("IN_PROGRESS", "COMPLETED"),
            ("IN_PROGRESS", "NEW"),
        }:
            self._invalid("Engineer cannot perform this transition")
        if new_status in {"IN_PROGRESS", "COMPLETED"} and context.assignment_id is None:
            self._invalid("Job has no current published assignment")
        if (
            context.old_status == "COMPLETED"
            and new_status == "IN_PROGRESS"
            and not (reason and reason.strip())
        ):
            self._invalid("Reason is required to return a completed job")
        if context.another_job_in_progress:
            self._invalid("Engineer already has a job in progress")
        if context.previous_job_unfinished:
            self._invalid("Previous route jobs must be completed or cancelled first")
        if context.later_job_started:
            self._invalid("A later route job has already started")

        return await self._repository.save_transition(
            context,
            new_status,
            user.id,
            reason.strip() if reason else None,
            deactivate_assignment=(
                context.old_status == "CANCELLED" and new_status == "NEW"
            ),
        )

    @staticmethod
    def _invalid(message: str) -> None:
        raise InvalidJobStatusError(message)
