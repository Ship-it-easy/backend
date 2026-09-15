from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from planning.domain.enums import ReasonCode


@dataclass(frozen=True)
class FutureOpportunity:
    count: int
    rank: int
    bonus: int


@dataclass(frozen=True)
class BatchJobDecision:
    job_id: int
    daily_drop_penalty: int
    opportunity: FutureOpportunity
    cascade_drop_penalty: int
    priority_group: str


@dataclass(frozen=True)
class PreparedPlanningBatch:
    id: int
    project_id: int
    requested_start_date: date
    effective_start_date: date
    maximum_horizon_end: date
    timezone: str
    input_hash: str
    configuration_version: str
    input_snapshot: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class PermanentIssue:
    job_id: int
    reason_code: ReasonCode
    diagnostic_flags: dict[str, Any]
