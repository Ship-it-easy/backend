from bisect import bisect_left
from datetime import date, time
from typing import Any

from planning.domain.entities.planning import PlanningConfig
from planning.domain.entities.planning_batch import FutureOpportunity


class FutureOpportunityCalendar:
    """Precomputed compatible engineer/day pairs for an immutable batch snapshot."""

    def __init__(
        self,
        jobs: list[dict[str, Any]],
        snapshot: dict[str, Any],
        maximum_end: date,
    ):
        self._dates_by_job = _opportunity_dates(jobs, snapshot, maximum_end)

    def get(
        self, job_id: int, current_date: date, config: PlanningConfig
    ) -> FutureOpportunity:
        dates = self._dates_by_job.get(job_id, ())
        count = len(dates) - bisect_left(dates, current_date)
        return _ranked_opportunity(count, config)


def future_opportunity(
    job: dict[str, Any],
    current_date: date,
    maximum_end: date,
    snapshot: dict[str, Any],
    config: PlanningConfig,
) -> FutureOpportunity:
    calendar = FutureOpportunityCalendar([job], snapshot, maximum_end)
    return calendar.get(int(job["id"]), current_date, config)


def _opportunity_dates(
    jobs: list[dict[str, Any]],
    snapshot: dict[str, Any],
    maximum_end: date,
) -> dict[int, tuple[date, ...]]:
    engineers = {int(item["id"]): item for item in snapshot["engineers"]}
    qualifications = {
        int(key): set(value)
        for key, value in snapshot["engineer_qualifications"].items()
    }
    equipment = {
        int(key): int(value) for key, value in snapshot["equipment_units"].items()
    }
    schedules = [
        (item, _date(item["work_date"]))
        for item in snapshot["schedules"]
        if _date(item["work_date"]) <= maximum_end
    ]
    result: dict[int, tuple[date, ...]] = {}
    for job in jobs:
        job_id = int(job["id"])
        required = set(
            snapshot["required_qualifications"].get(
                str(job["work_type_id"]), []
            )
        )
        required_equipment = set(
            snapshot["required_equipment"].get(str(job["work_type_id"]), [])
        )
        duration = job.get("service_duration_min") or job.get(
            "default_service_duration_min"
        )
        window_start = _minute(job.get("time_window_start"), start=True)
        window_end = _minute(job.get("time_window_end"), start=False)
        if (
            any(equipment.get(item, 0) <= 0 for item in required_equipment)
            or not duration
            or int(duration) <= 0
            or window_start > window_end
        ):
            result[job_id] = ()
            continue
        required_transport = job.get("required_transport")
        compatible_dates = []
        for schedule, work_date in schedules:
            engineer = engineers.get(int(schedule["engineer_id"]))
            if engineer is None:
                continue
            if not required.issubset(
                qualifications.get(int(engineer["id"]), set())
            ):
                continue
            if required_transport == "CAR" and engineer["transport_type"] != "CAR":
                continue
            shift_start = _minute(schedule["shift_start"], start=True)
            shift_end = _minute(schedule["shift_end"], start=False)
            earliest = max(shift_start, window_start)
            latest = min(shift_end, window_end + int(duration))
            if earliest + int(duration) <= latest:
                compatible_dates.append(work_date)
        result[job_id] = tuple(sorted(compatible_dates))
    return result


def _ranked_opportunity(
    count: int, config: PlanningConfig
) -> FutureOpportunity:
    if count == 0:
        return FutureOpportunity(0, 0, 0)
    if count == 1:
        return FutureOpportunity(count, 3, config.future_opportunity_critical)
    if count <= 3:
        return FutureOpportunity(count, 2, config.future_opportunity_high)
    if count <= 7:
        return FutureOpportunity(count, 1, config.future_opportunity_limited)
    return FutureOpportunity(count, 0, 0)


def permanent_issue(
    job: dict[str, Any],
    snapshot: dict[str, Any],
    effective_start: date,
    maximum_end: date,
) -> tuple[str, dict[str, Any]] | None:
    duration = job.get("service_duration_min") or job.get(
        "default_service_duration_min"
    )
    if not duration or int(duration) <= 0:
        return "MISSING_SERVICE_DURATION", {}
    start = _minute(job.get("time_window_start"), start=True)
    end = _minute(job.get("time_window_end"), start=False)
    if start > end:
        return "INVALID_TIME_WINDOW_FOR_HORIZON", {
            "window_start_min": start,
            "window_end_min": end,
        }
    required_equipment = snapshot["required_equipment"].get(
        str(job["work_type_id"]), []
    )
    equipment = snapshot["equipment_units"]
    missing = [
        item for item in required_equipment if int(equipment.get(str(item), 0)) <= 0
    ]
    if missing:
        return "EQUIPMENT_UNAVAILABLE_IN_HORIZON", {"equipment_type_ids": missing}
    schedules = [
        item
        for item in snapshot["schedules"]
        if effective_start <= _date(item["work_date"]) <= maximum_end
    ]
    if not schedules:
        return "NO_SHIFT_IN_HORIZON", {
            "effective_start": effective_start.isoformat(),
            "maximum_end": maximum_end.isoformat(),
        }
    engineers = {int(item["id"]): item for item in snapshot["engineers"]}
    qualifications = {
        int(key): set(value)
        for key, value in snapshot["engineer_qualifications"].items()
    }
    required = set(
        snapshot["required_qualifications"].get(str(job["work_type_id"]), [])
    )
    required_transport = job.get("required_transport")
    compatible = []
    for schedule in schedules:
        engineer = engineers.get(int(schedule["engineer_id"]))
        if engineer is None:
            continue
        if not required.issubset(qualifications.get(int(engineer["id"]), set())):
            continue
        if required_transport == "CAR" and engineer["transport_type"] != "CAR":
            continue
        compatible.append(schedule)
    if not compatible:
        return "NO_COMPATIBLE_ENGINEER_IN_HORIZON", {
            "required_qualification_ids": sorted(required),
            "required_transport": required_transport,
        }
    fitting_duration = [
        item
        for item in compatible
        if _minute(item["shift_end"], start=False)
        - _minute(item["shift_start"], start=True)
        >= int(duration)
    ]
    if not fitting_duration:
        return "DURATION_EXCEEDS_ALL_SHIFTS", {
            "required_minutes": int(duration),
        }
    for schedule in fitting_duration:
        shift_start = _minute(schedule["shift_start"], start=True)
        shift_end = _minute(schedule["shift_end"], start=False)
        if max(shift_start, start) + int(duration) <= min(
            shift_end, end + int(duration)
        ):
            return None
    return "INVALID_TIME_WINDOW_FOR_HORIZON", {
        "window_start_min": start,
        "window_end_min": end,
        "required_minutes": int(duration),
    }


def priority_group(sla_date: date, planning_date: date, block_end: date) -> str:
    days = (sla_date - planning_date).days
    if days < 0:
        return "OVERDUE"
    if days == 0:
        return "DUE_TODAY"
    if days == 1:
        return "DUE_IN_1_DAY"
    if days <= 3:
        return "DUE_IN_2_3_DAYS"
    if sla_date <= block_end:
        return "DUE_LATER_IN_CURRENT_BLOCK"
    return "RESERVE"


def _date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _minute(value: time | str | None, *, start: bool) -> int:
    if value is None:
        return 0 if start else 1439
    if isinstance(value, str):
        value = time.fromisoformat(value)
    return value.hour * 60 + value.minute + (int(bool(value.second)) if start else 0)
