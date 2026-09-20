import math
from dataclasses import asdict, replace
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from planning.application.errors import PlanningUnavailable
from planning.application.interfaces.geocoder import Geocoder
from planning.domain.entities.coordinate import Coordinate
from planning.domain.entities.engineer import Engineer
from planning.domain.entities.job import Job, UnassignedJob
from planning.domain.entities.planning import PlanningConfig, PlanningInput
from planning.domain.enums import JobPriorityType, ReasonCode, TransportType

EMERGENCY_BONUS = 1_000_000


class PlanningInputNormalizer:
    def __init__(self, geocoder: Geocoder):
        self._geocoder = geocoder

    async def normalize(
        self,
        project_id: int,
        planning_date: date,
        timezone_name: str,
        source: dict[str, Any],
    ) -> PlanningInput:
        if source["config"] is None:
            raise PlanningUnavailable("Project has no active planning configuration")
        config_values = {
            field: source["config"][field]
            for field in PlanningConfig.__dataclass_fields__
            if field in source["config"]
        }
        if config_values.get("travel_cache_ttl_days") is None:
            config_values["travel_cache_ttl_days"] = 7
        config = PlanningConfig(**config_values)
        equipment_units = source["equipment_units"]
        local_today = (
            datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
        )
        engineers = await self._normalize_engineers(
            source["engineers"],
            source,
            (
                current_minute_ceil(timezone_name)
                if planning_date == local_today
                else None
            ),
        )
        jobs, pre_unassigned = await self._normalize_jobs(
            source["jobs"], source, planning_date, config
        )

        compatible_counts: dict[int, int] = {}
        compatible_by_job: dict[int, list[int]] = {}
        eligible_counts: dict[int, int] = {}
        for job in jobs:
            compatible = [
                engineer.id
                for engineer in engineers
                if is_base_compatible(job, engineer)
            ]
            compatible_by_job[job.id] = compatible
            compatible_counts[job.id] = len(compatible)
            eligible_counts[job.id] = sum(
                1
                for engineer in engineers
                if engineer.id in compatible and _fits_daily_window(job, engineer)
            )

        penalty_components = {
            job.id: calculate_drop_penalty_components(
                job,
                planning_date,
                config,
                compatible_counts[job.id],
                equipment_units,
            )
            for job in jobs
        }
        jobs_with_penalty = [
            replace(
                job,
                drop_penalty=penalty_components[job.id]["daily_drop_penalty_v2"],
            )
            for job in jobs
        ]
        eligible: list[Job] = []
        if not engineers:
            pre_unassigned.extend(
                UnassignedJob(
                    job_id=job.id,
                    drop_penalty=job.drop_penalty,
                    reason_code=ReasonCode.NO_AVAILABLE_ENGINEER,
                    diagnostic_flags={
                        "planning_date": planning_date.isoformat(),
                    },
                )
                for job in jobs_with_penalty
            )
        else:
            for job in jobs_with_penalty:
                if not compatible_by_job[job.id]:
                    pre_unassigned.append(
                        UnassignedJob(
                            job_id=job.id,
                            drop_penalty=job.drop_penalty,
                            reason_code=ReasonCode.NO_COMPATIBLE_ENGINEER,
                            diagnostic_flags={
                                "required_qualification_ids": sorted(
                                    job.required_qualifications
                                ),
                                "required_equipment_type_ids": sorted(
                                    job.required_equipment
                                ),
                                "required_transport": (
                                    job.required_transport.value
                                    if job.required_transport
                                    else None
                                ),
                            },
                        )
                    )
                elif not any(
                    _fits_daily_window(job, engineer)
                    for engineer in engineers
                    if engineer.id in compatible_by_job[job.id]
                ):
                    # A dropped RoutingModel node still has a Time dimension.
                    # Keeping an already expired or otherwise impossible window
                    # in the model can make the whole solve fail instead of
                    # returning the job as unassigned.
                    pre_unassigned.append(
                        UnassignedJob(
                            job_id=job.id,
                            drop_penalty=job.drop_penalty,
                            reason_code=ReasonCode.DAILY_TIME_WINDOW_CONFLICT,
                            diagnostic_flags={
                                "window_start_min": job.window_start_min,
                                "window_end_min": job.window_end_min,
                                "required_minutes": job.duration_min,
                            },
                        )
                    )
                else:
                    eligible.append(job)

        eligible.sort(
            key=lambda job: (
                -job.drop_penalty,
                job.sla_date,
                job.created_at,
                job.id,
            )
        )
        limited = eligible[: config.max_jobs_per_run]
        pre_unassigned.extend(
            UnassignedJob(
                job_id=job.id,
                drop_penalty=job.drop_penalty,
                reason_code=ReasonCode.DATASET_LIMIT,
                diagnostic_flags={
                    "max_jobs_per_run": config.max_jobs_per_run,
                    "final_penalty": job.drop_penalty,
                },
            )
            for job in eligible[config.max_jobs_per_run :]
        )
        snapshot = {
            "project_id": project_id,
            "planning_date": planning_date,
            "timezone": timezone_name,
            "jobs": [asdict(job) for job in jobs_with_penalty],
            "engineers": [asdict(engineer) for engineer in engineers],
            "equipment_units": equipment_units,
            "penalty_components": {
                str(job_id): values for job_id, values in penalty_components.items()
            },
            "eligible_engineer_counts": {
                str(job_id): value for job_id, value in eligible_counts.items()
            },
        }
        return PlanningInput(
            project_id=project_id,
            planning_date=planning_date,
            timezone=timezone_name,
            config=config,
            jobs=limited,
            engineers=engineers,
            equipment_units=equipment_units,
            pre_unassigned=pre_unassigned,
            input_jobs_count=len(source["jobs"]),
            sla_critical_job_ids=frozenset(
                job.id for job in jobs_with_penalty if job.sla_date <= planning_date
            ),
            snapshot=snapshot,
            preallocated_equipment_by_engineer={
                int(engineer_id): frozenset(int(value) for value in equipment_ids)
                for engineer_id, equipment_ids in source.get(
                    "preallocated_equipment_by_engineer", {}
                ).items()
            },
        )

    async def _normalize_engineers(
        self,
        rows: list[dict[str, Any]],
        source: dict[str, Any],
        current_minute: int | None,
    ) -> list[Engineer]:
        result = []
        for row in rows:
            shift_start_min = _time_to_start_minute(row["shift_start"])
            shift_end_min = _time_to_end_minute(row["shift_end"])
            if current_minute is not None:
                shift_start_min = max(shift_start_min, current_minute)
            if shift_start_min >= shift_end_min:
                continue
            coordinate = await self._coordinate(
                row["start_latitude"],
                row["start_longitude"],
                row["start_address"],
            )
            if coordinate is None:
                raise PlanningUnavailable(
                    f"Engineer {row['engineer_id']} start location cannot be geocoded"
                )
            result.append(
                Engineer(
                    id=int(row["engineer_id"]),
                    transport_type=TransportType(row["transport_type"]),
                    coordinate=coordinate,
                    shift_start_min=shift_start_min,
                    shift_end_min=shift_end_min,
                    qualifications=frozenset(
                        source["engineer_qualifications"].get(
                            int(row["engineer_id"]), set()
                        )
                    ),
                )
            )
        return result

    async def _normalize_jobs(
        self,
        rows: list[dict[str, Any]],
        source: dict[str, Any],
        planning_date: date,
        config: PlanningConfig,
    ) -> tuple[list[Job], list[UnassignedJob]]:
        jobs: list[Job] = []
        invalid: list[UnassignedJob] = []
        for row in rows:
            job_id = int(row["jobs_id"] if "jobs_id" in row else row["id"])
            duration = (
                row["service_duration_min"] or row["default_service_duration_min"]
            )
            reason: ReasonCode | None = None
            if not duration or duration <= 0:
                reason = ReasonCode.MISSING_SERVICE_DURATION
            start = row["time_window_start"]
            end = row["time_window_end"]
            if start is not None and end is not None and start > end:
                reason = ReasonCode.INVALID_TIME_WINDOW
            window_start_min = 0 if start is None else _time_to_start_minute(start)
            window_end_min = 1439 if end is None else _time_to_end_minute(end)
            if window_start_min > window_end_min:
                reason = ReasonCode.INVALID_TIME_WINDOW
            coordinate = await self._coordinate(
                row["latitude"], row["longitude"], row["address"]
            )
            if coordinate is None and reason is None:
                reason = ReasonCode.GEOCODING_FAILED
            if reason is not None:
                invalid.append(
                    UnassignedJob(
                        job_id=job_id,
                        drop_penalty=_sla_penalty(
                            row["sla_date"], planning_date, config
                        ),
                        reason_code=reason,
                    )
                )
                continue
            work_type_id = int(row["work_type_id"])
            jobs.append(
                Job(
                    id=job_id,
                    sla_date=row["sla_date"],
                    duration_min=int(duration),
                    coordinate=coordinate,
                    window_start_min=window_start_min,
                    window_end_min=window_end_min,
                    required_transport=(
                        TransportType(row["required_transport"])
                        if row["required_transport"]
                        else None
                    ),
                    required_qualifications=frozenset(
                        source["required_qualifications"].get(work_type_id, set())
                    ),
                    required_equipment=frozenset(
                        source["required_equipment"].get(work_type_id, set())
                    ),
                    created_at=row["created_at"],
                    allowed_engineer_ids=(
                        frozenset(int(value) for value in row["allowed_engineer_ids"])
                        if row.get("allowed_engineer_ids") is not None
                        else None
                    ),
                    mandatory=bool(row.get("mandatory", False)),
                    priority_type=JobPriorityType(
                        row.get("priority_type", JobPriorityType.NORMAL)
                    ),
                )
            )
        return jobs, invalid

    async def _coordinate(
        self,
        latitude: Decimal | float | None,
        longitude: Decimal | float | None,
        address: str | None,
    ) -> Coordinate | None:
        if latitude is not None and longitude is not None:
            return Coordinate(float(latitude), float(longitude))
        if address:
            return await self._geocoder.geocode(address)
        return None


def is_base_compatible(job: Job, engineer: Engineer) -> bool:
    if (
        job.allowed_engineer_ids is not None
        and engineer.id not in job.allowed_engineer_ids
    ):
        return False
    if not job.required_qualifications.issubset(engineer.qualifications):
        return False
    return not (
        job.required_transport == TransportType.CAR
        and engineer.transport_type != TransportType.CAR
    )


def _fits_daily_window(job: Job, engineer: Engineer) -> bool:
    earliest_start = max(job.window_start_min, engineer.shift_start_min)
    latest_start = min(
        job.window_end_min,
        engineer.shift_end_min - job.duration_min,
    )
    return earliest_start <= latest_start


def calculate_drop_penalty(
    job: Job,
    planning_date: date,
    config: PlanningConfig,
    compatible_count: int,
    equipment_units: dict[int, int],
) -> int:
    return calculate_drop_penalty_components(
        job,
        planning_date,
        config,
        compatible_count,
        equipment_units,
    )["daily_drop_penalty_v2"]


def calculate_drop_penalty_components(
    job: Job,
    planning_date: date,
    config: PlanningConfig,
    compatible_count: int,
    equipment_units: dict[int, int],
) -> dict[str, int]:
    days = (job.sla_date - planning_date).days
    sla_base = (
        config.sla_overdue_base
        if days < 0
        else _sla_penalty(job.sla_date, planning_date, config)
    )
    overdue_bonus = config.sla_overdue_per_day * -days if days < 0 else 0
    emergency_bonus = (
        EMERGENCY_BONUS
        if job.priority_type == JobPriorityType.EMERGENCY
        else 0
    )
    skill_scarcity_bonus = 0
    if compatible_count == 1:
        skill_scarcity_bonus = config.skill_one_engineer
    elif compatible_count == 2:
        skill_scarcity_bonus = config.skill_two_engineers
    equipment_inventory_bonus = 0
    for equipment_id in job.required_equipment:
        units = equipment_units.get(equipment_id, 0)
        if units == 1:
            equipment_inventory_bonus += config.equipment_one_unit
        elif units == 2:
            equipment_inventory_bonus += config.equipment_two_units
    window_size = job.window_end_min - job.window_start_min
    window_tightness_bonus = 0
    if window_size <= 30:
        window_tightness_bonus = config.window_30
    elif window_size <= 60:
        window_tightness_bonus = config.window_60
    elif window_size <= 120:
        window_tightness_bonus = config.window_120
    daily_drop_penalty = sum(
        (
            sla_base,
            overdue_bonus,
            skill_scarcity_bonus,
            equipment_inventory_bonus,
            window_tightness_bonus,
            emergency_bonus,
        )
    )
    return {
        "sla_base": sla_base,
        "overdue_bonus": overdue_bonus,
        "skill_scarcity_bonus": skill_scarcity_bonus,
        "equipment_inventory_bonus": equipment_inventory_bonus,
        "window_tightness_bonus": window_tightness_bonus,
        "emergency_bonus": emergency_bonus,
        "daily_drop_penalty_v2": daily_drop_penalty,
    }


def _sla_penalty(sla_date: date, planning_date: date, config: PlanningConfig) -> int:
    days = (sla_date - planning_date).days
    if days < 0:
        return config.sla_overdue_base + config.sla_overdue_per_day * -days
    if days == 0:
        return config.sla_today
    if days == 1:
        return config.sla_tomorrow
    if days <= 3:
        return config.sla_2_3_days
    return config.sla_later


def _time_to_start_minute(value: time) -> int:
    return value.hour * 60 + value.minute + math.ceil(value.second / 60)


def _time_to_end_minute(value: time) -> int:
    return value.hour * 60 + value.minute


def current_minute_ceil(timezone_name: str) -> int:
    local_now = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name))
    return (
        local_now.hour * 60
        + local_now.minute
        + int(local_now.second > 0 or local_now.microsecond > 0)
    )
