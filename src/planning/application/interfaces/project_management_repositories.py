from datetime import date
from typing import Any, Protocol

from planning.application.management_dto import (
    PlanningPublicationState,
    PlanningReadinessState,
    ProjectJobEditState,
    PublishedPlan,
    PublishPlanCommand,
    WorkTypeEditState,
)


class ProjectCatalogRepository(Protocol):
    async def list_catalog(
        self, project_id: int, kind: str
    ) -> list[dict[str, Any]]: ...
    async def create_catalog(
        self, project_id: int, kind: str, values: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def update_catalog(
        self, project_id: int, kind: str, item_id: int, values: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def clear_equipment(
        self, project_id: int, item_id: int
    ) -> dict[str, Any]: ...
    async def list_work_types(self, project_id: int) -> list[dict[str, Any]]: ...
    async def create_work_type(
        self,
        project_id: int,
        values: dict[str, Any],
        qualification_ids: list[int],
        equipment_type_ids: list[int],
    ) -> dict[str, Any]: ...
    async def update_work_type(
        self,
        project_id: int,
        item_id: int,
        values: dict[str, Any],
        qualification_ids: list[int] | None,
        equipment_type_ids: list[int] | None,
    ) -> dict[str, Any]: ...


class EngineerManagementRepository(Protocol):
    async def list_engineers(self, project_id: int) -> list[dict[str, Any]]: ...
    async def create_engineer(
        self, project_id: int, values: dict[str, Any], qualification_ids: list[int]
    ) -> dict[str, Any]: ...
    async def get_engineer(
        self, project_id: int, engineer_id: int
    ) -> dict[str, Any]: ...
    async def update_engineer(
        self,
        project_id: int,
        engineer_id: int,
        values: dict[str, Any],
        qualification_ids: list[int] | None,
        actor_user_id: Any,
    ) -> dict[str, Any]: ...
    async def replace_schedule(
        self, project_id: int, engineer_id: int, entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]: ...
    async def replace_schedule_with_event(
        self,
        project_id: int,
        engineer_id: int,
        entries: list[dict[str, Any]],
        actor_user_id: Any,
    ) -> dict[str, Any]: ...


class EngineerAccountRepository(Protocol):
    async def engineer_belongs_to_project(
        self, project_id: int, engineer_id: int
    ) -> bool: ...
    async def create_account(
        self, project_id: int, engineer_id: int, login: str, password_hash: str
    ) -> dict[str, Any]: ...
    async def reset_password(
        self, project_id: int, engineer_id: int, password_hash: str
    ) -> dict[str, str]: ...
    async def set_active(
        self,
        project_id: int,
        engineer_id: int,
        active: bool,
        actor_user_id: Any,
    ) -> dict[str, Any]: ...


class ProjectJobsRepository(Protocol):
    async def list_jobs(
        self, project_id: int, filters: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def create_job(
        self, project_id: int, values: dict[str, Any], actor_user_id: Any
    ) -> dict[str, Any]: ...
    async def import_jobs(
        self,
        project_id: int,
        rows: list[dict[str, Any]],
        actor_user_id: Any,
    ) -> dict[str, Any]: ...
    async def get_job(self, project_id: int, job_id: int) -> dict[str, Any]: ...
    async def load_job_for_update(
        self, project_id: int, job_id: int
    ) -> ProjectJobEditState: ...
    async def get_work_type_for_edit(
        self, work_type_id: int
    ) -> WorkTypeEditState | None: ...
    async def save_job(self, job_id: int, values: dict[str, Any]) -> dict[str, Any]: ...
    async def cancel_job(
        self, project_id: int, job_id: int, actor_user_id: Any
    ) -> dict[str, Any]: ...


class PlanningManagementRepository(Protocol):
    async def get_config(self, project_id: int) -> dict[str, Any]: ...
    async def update_config(
        self, project_id: int, values: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def get_readiness_state(
        self, project_id: int, planning_date: date
    ) -> PlanningReadinessState: ...
    async def load_publication_state(
        self, project_id: int, run_id: int
    ) -> PlanningPublicationState | None: ...
    async def validate_batch_for_publication(
        self, project_id: int, batch_id: int
    ) -> bool: ...
    async def save_publication(self, command: PublishPlanCommand) -> PublishedPlan: ...
    async def get_daily_plan(
        self, project_id: int, planning_date: date
    ) -> dict[str, Any]: ...


class AddressSearchProvider(Protocol):
    async def search(
        self, query: str, *, require_house: bool = True
    ) -> list[dict[str, Any]]: ...
