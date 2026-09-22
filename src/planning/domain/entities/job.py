from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from planning.domain.entities.coordinate import Coordinate
from planning.domain.enums import ReasonCode, TransportType, WorkPriority


@dataclass(frozen=True)
class Job:
    id: int
    sla_date: date
    duration_min: int
    coordinate: Coordinate
    window_start_min: int
    window_end_min: int
    required_transport: TransportType | None
    required_qualifications: frozenset[int]
    required_equipment: frozenset[int]
    created_at: datetime
    drop_penalty: int = 0
    allowed_engineer_ids: frozenset[int] | None = None
    mandatory: bool = False
    priority: WorkPriority = WorkPriority.LOW


@dataclass
class UnassignedJob:
    job_id: int
    drop_penalty: int
    reason_code: ReasonCode
    diagnostic_flags: dict[str, Any] = field(default_factory=dict)
