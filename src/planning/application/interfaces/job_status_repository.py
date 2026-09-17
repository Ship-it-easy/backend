from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class JobStatusContext:
    job_id: int
    old_status: str
    assignment_id: int | None
    assignment_engineer_id: int | None
    assignment_matches_engineer: bool
    another_job_in_progress: bool
    previous_job_unfinished: bool
    later_job_started: bool
    project_assignment_id: int | None = None


class JobStatusRepository(Protocol):
    async def load_transition_context(
        self,
        job_id: int,
        project_id: int,
        engineer_id: int | None,
        new_status: str,
    ) -> JobStatusContext: ...

    async def save_transition(
        self,
        context: JobStatusContext,
        new_status: str,
        actor_user_id: UUID,
        reason: str | None,
        *,
        deactivate_assignment: bool,
    ) -> dict[str, Any]: ...
