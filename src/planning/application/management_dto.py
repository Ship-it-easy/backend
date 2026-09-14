from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from auth.domain.user_role import UserRoleEnum


@dataclass(frozen=True, slots=True)
class UserActivationState:
    id: UUID
    login: str
    role: UserRoleEnum
    project_id: int | None
    engineer_id: int | None
    active: bool
    active_owner_count: int

    def response(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "login": self.login,
            "role": self.role.value,
            "project_id": self.project_id,
            "engineer_id": self.engineer_id,
            "status": "ACTIVE" if self.active else "BLOCKED",
        }


@dataclass(frozen=True, slots=True)
class ProjectUserValidationState:
    project_exists: bool
    project_active: bool
    engineer_belongs_to_project: bool | None = None


@dataclass(frozen=True, slots=True)
class AdminProjectUpdateState:
    exists: bool
    has_planning_runs: bool


@dataclass(frozen=True, slots=True)
class PlanningReadinessState:
    has_config: bool
    has_work_types: bool
    has_engineers: bool
    has_jobs: bool


@dataclass(frozen=True, slots=True)
class ProjectJobEditState:
    id: int
    project_id: int
    status: str
    published: bool
    work_type_id: int
    service_duration_min: int | None
    address: str
    latitude: Decimal | float | None
    longitude: Decimal | float | None
    time_window_start: time | None
    time_window_end: time | None


@dataclass(frozen=True, slots=True)
class WorkTypeEditState:
    id: int
    project_id: int
    active: bool
    default_service_duration_min: int | None


@dataclass(frozen=True, slots=True)
class AssignmentSnapshot:
    required_qualifications: tuple[int, ...] = ()
    required_equipment: tuple[int, ...] = ()
    required_transport: str | None = None


@dataclass(frozen=True, slots=True)
class PlanningRouteAssignment:
    job_id: int
    engineer_id: int
    sequence: int
    planned_arrival: datetime
    planned_start: datetime
    planned_finish: datetime
    travel_from_previous_min: int
    waiting_before_job_min: int


@dataclass(frozen=True, slots=True)
class PlanningPublicationState:
    run_id: int
    planning_date: date
    status: str
    validation_errors: tuple[Any, ...]
    unassigned_jobs_count: int
    already_published: bool
    daily_plan_id: int | None
    current_version_id: int | None
    current_plan_started: bool
    max_version_number: int
    routes: tuple[PlanningRouteAssignment, ...]
    publishable_job_ids: frozenset[int]
    snapshots: dict[int, AssignmentSnapshot] = field(default_factory=dict)
    batch_status: str | None = None
    batch_stale_for_publication: bool = False
    batch_current: bool = False
    timezone: str = "UTC"
    batch_id: int | None = None


@dataclass(frozen=True, slots=True)
class PublishPlanCommand:
    project_id: int
    run_id: int
    planning_date: date
    published_by: UUID
    daily_plan_id: int | None
    previous_version_id: int | None
    version_number: int
    assignments: tuple[PlanningRouteAssignment, ...]
    snapshots: dict[int, AssignmentSnapshot]


@dataclass(frozen=True, slots=True)
class PublishedPlan:
    daily_plan_id: int
    plan_version_id: int
    version_number: int
    assignments_count: int

    def response(self) -> dict[str, Any]:
        return {
            "daily_plan_id": self.daily_plan_id,
            "plan_version_id": self.plan_version_id,
            "version_number": self.version_number,
            "status": "PUBLISHED",
            "assignments_count": self.assignments_count,
        }
