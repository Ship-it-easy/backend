from abc import abstractmethod
from datetime import date
from typing import Any, Protocol

from planning.domain.entities.planning import PlanningInput, PlanningResult


class PlanningBatchRepository(Protocol):
    @abstractmethod
    async def get_project_timezone(self, project_id: int) -> str: ...

    @abstractmethod
    async def create_or_reuse(
        self,
        project_id: int,
        requested_start_date: date,
        initiated_by_user_id: Any,
        idempotency_key: str,
        *,
        effective_start_override: date | None = None,
        excluded_job_ids: set[int] | None = None,
        include_published_jobs: bool = False,
        total_time_limit_override: int | None = None,
    ) -> tuple[dict[str, Any], bool]: ...

    @abstractmethod
    async def load_for_execution(self, batch_id: int) -> dict[str, Any] | None: ...

    @abstractmethod
    async def mark_batch_status(self, batch_id: int, status: str) -> None: ...

    @abstractmethod
    async def should_stop(self, batch_id: int) -> bool: ...

    @abstractmethod
    async def create_days(self, batch_id: int, dates: list[date]) -> None: ...

    @abstractmethod
    async def mark_day_running(
        self, batch_id: int, planning_date: date, input_job_ids: list[int]
    ) -> None: ...

    @abstractmethod
    async def create_daily_run(
        self, batch_id: int, planning_date: date, timezone_name: str, user_id: Any
    ) -> int: ...

    @abstractmethod
    async def mark_daily_run_running(
        self, run_id: int, data: PlanningInput
    ) -> None: ...

    @abstractmethod
    async def save_day_result(
        self,
        batch_id: int,
        run_id: int,
        data: PlanningInput,
        result: PlanningResult,
        decisions: dict[int, dict[str, Any]],
    ) -> set[int]: ...

    @abstractmethod
    async def skip_day(
        self, batch_id: int, planning_date: date, remaining_count: int
    ) -> None: ...

    @abstractmethod
    async def fail_day(
        self,
        batch_id: int,
        planning_date: date,
        run_id: int | None,
        code: str,
        message: str,
    ) -> None: ...

    @abstractmethod
    async def set_permanent_issues(
        self, batch_id: int, issues: dict[int, tuple[str, dict[str, Any]]]
    ) -> None: ...

    @abstractmethod
    async def validate_terminal_state(
        self, batch_id: int, status: str, completion_reason: str
    ) -> list[str]: ...

    @abstractmethod
    async def finish_batch(
        self,
        batch_id: int,
        status: str,
        completion_reason: str,
        remaining_job_ids: set[int],
        metrics: dict[str, Any],
    ) -> None: ...

    @abstractmethod
    async def fail_batch(self, batch_id: int, code: str, message: str) -> None: ...

    @abstractmethod
    async def get_batch(self, project_id: int, batch_id: int) -> dict[str, Any]: ...

    @abstractmethod
    async def list_batches(
        self,
        project_id: int,
        status: str | None,
        date_from: date | None,
        date_to: date | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def request_stop(self, project_id: int, batch_id: int) -> dict[str, Any]: ...

    @abstractmethod
    async def validate_current_day(
        self, project_id: int, batch_id: int
    ) -> dict[str, Any]: ...


class PlanningBatchExecutor(Protocol):
    @abstractmethod
    def schedule(self, batch_id: int) -> None: ...

    @abstractmethod
    def schedule_project(self, project_id: int) -> None: ...

    @abstractmethod
    async def recover(self) -> None: ...

    @abstractmethod
    async def shutdown(self) -> None: ...
