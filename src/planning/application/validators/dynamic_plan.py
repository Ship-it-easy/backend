from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from planning.application.services.today_route_protection import (
    TodayRouteProtectionService,
)


class DynamicPlanValidator:
    """Validate the composed plan, independently from the daily solver."""

    _protected_fields = (
        "engineer_id",
        "planning_date",
        "sequence",
        "planned_arrival",
        "planned_start",
        "planned_finish",
    )

    def validate_today(
        self,
        context: dict[str, Any],
        proposed_assignments: list[dict[str, Any]],
        planning_date: date,
    ) -> list[str]:
        errors: list[str] = []
        timezone_name = str(context["source"]["project"]["timezone"])
        original = [_typed_assignment(item) for item in context["today_assignments"]]
        proposed = [_typed_assignment(item) for item in proposed_assignments]
        proposed_by_job = {int(item["job_id"]): item for item in proposed}
        routes: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for item in original:
            routes[int(item["engineer_id"])].append(item)
        schedules = {
            int(item["engineer_id"]): item
            for item in context["source"]["schedules"]
            if _date(item["work_date"]) == planning_date
        }
        protection_service = TodayRouteProtectionService()
        for engineer_id, route in routes.items():
            schedule = schedules.get(engineer_id)
            # A deleted schedule must not make an already executing route mutable.
            shift_start_minute = (
                _minute(_time(schedule["shift_start"])) if schedule is not None else 0
            )
            protection = protection_service.build(
                engineer_id,
                route,
                shift_start_minute,
                _datetime(context["snapshot_time"]),
                timezone_name,
            )
            fixed_ids = {int(item["job_id"]) for item in protection.fixed}
            for before in protection.fixed:
                job_id = int(before["job_id"])
                after = proposed_by_job.get(job_id)
                if after is None:
                    errors.append(f"protected job {job_id} is missing")
                    continue
                for field in self._protected_fields:
                    if _comparable(before.get(field)) != _comparable(after.get(field)):
                        errors.append(f"protected job {job_id} changed {field}")
            for before in route:
                job_id = int(before["job_id"])
                if job_id in fixed_ids or before.get("status") != "NEW":
                    continue
                after = proposed_by_job.get(job_id)
                if after is not None and int(after["engineer_id"]) != engineer_id:
                    errors.append(f"today job {job_id} moved to another engineer")

        errors.extend(
            self.validate_publication(
                proposed,
                timezone_name=timezone_name,
                minimum_date=planning_date,
                maximum_date=planning_date,
            )
        )
        return errors

    def validate_publication(
        self,
        assignments: list[dict[str, Any]],
        *,
        timezone_name: str,
        minimum_date: date,
        maximum_date: date,
    ) -> list[str]:
        errors: list[str] = []
        timezone = ZoneInfo(timezone_name)
        typed = [_typed_assignment(item) for item in assignments]
        job_ids = [int(item["job_id"]) for item in typed]
        if len(job_ids) != len(set(job_ids)):
            errors.append("a job is assigned more than once")

        routes: dict[tuple[date, int], list[dict[str, Any]]] = defaultdict(list)
        for item in typed:
            planning_date = _date(item["planning_date"])
            job_id = int(item["job_id"])
            if not minimum_date <= planning_date <= maximum_date:
                errors.append(f"job {job_id} is outside publication horizon")
            arrival = _datetime(item["planned_arrival"])
            start = _datetime(item["planned_start"])
            finish = _datetime(item["planned_finish"])
            if any(
                value.astimezone(timezone).date() != planning_date
                for value in (arrival, start, finish)
            ):
                errors.append(f"job {job_id} has timestamps on another local date")
            if not arrival <= start < finish:
                errors.append(f"job {job_id} has an invalid time interval")
            waiting = int((start - arrival).total_seconds() // 60)
            if waiting != int(item.get("waiting_before_job_min") or 0):
                errors.append(f"job {job_id} has an invalid waiting time")
            routes[(planning_date, int(item["engineer_id"]))].append(item)

        for (planning_date, engineer_id), route in routes.items():
            ordered = sorted(
                route, key=lambda item: (int(item["sequence"]), int(item["job_id"]))
            )
            sequences = [int(item["sequence"]) for item in ordered]
            if sequences != list(range(1, len(ordered) + 1)):
                errors.append(
                    f"route {engineer_id} on {planning_date} has invalid sequence"
                )
            for previous, current in zip(ordered, ordered[1:], strict=False):
                expected_arrival = _datetime(previous["planned_finish"]) + timedelta(
                    minutes=int(current.get("travel_from_previous_min") or 0)
                )
                if expected_arrival != _datetime(current["planned_arrival"]):
                    errors.append(
                        f"route {engineer_id} on {planning_date} "
                        "has broken travel chain"
                    )
        return errors


def _typed_assignment(item: dict[str, Any]) -> dict[str, Any]:
    value = dict(item)
    value["planning_date"] = _date(value["planning_date"])
    for field in (
        "planned_arrival",
        "planned_start",
        "planned_finish",
        "actual_started_at",
        "actual_completed_at",
    ):
        if value.get(field) is not None:
            value[field] = _datetime(value[field])
    return value


def _comparable(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return _datetime(value)
        except ValueError:
            try:
                return _date(value)
            except ValueError:
                return value
    return value


def _date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _datetime(value: datetime | str) -> datetime:
    return (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _time(value: time | str) -> time:
    return value if isinstance(value, time) else time.fromisoformat(value)


def _minute(value: time) -> int:
    return value.hour * 60 + value.minute
