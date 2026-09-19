from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class ProtectedRoute:
    engineer_id: int
    fixed: tuple[dict[str, Any], ...]
    mutable: tuple[dict[str, Any], ...]
    boundary: dict[str, Any] | None
    boundary_time: datetime
    diagnostics: tuple[str, ...]


class TodayRouteProtectionService:
    """Derive the immutable prefix and same-engineer mutable tail for today."""

    def build(
        self,
        engineer_id: int,
        assignments: list[dict[str, Any]],
        shift_start_minute: int,
        snapshot_time: datetime,
        timezone_name: str,
        *,
        protect_next: bool = True,
    ) -> ProtectedRoute:
        ordered = sorted(
            assignments, key=lambda item: (item["sequence"], item["job_id"])
        )
        local_now = snapshot_time.astimezone(ZoneInfo(timezone_name))
        minute = local_now.hour * 60 + local_now.minute
        completed_indexes = [
            index for index, item in enumerate(ordered) if item["status"] == "COMPLETED"
        ]
        fixed_end = max(completed_indexes, default=-1)
        in_progress = next(
            (
                index
                for index, item in enumerate(ordered)
                if item["status"] == "IN_PROGRESS"
            ),
            None,
        )
        diagnostics: list[str] = []
        if in_progress is not None:
            fixed_end = max(fixed_end, in_progress)
            next_unfinished = next(
                (
                    index
                    for index in range(in_progress + 1, len(ordered))
                    if ordered[index]["status"] != "COMPLETED"
                ),
                None,
            )
            if protect_next and next_unfinished is not None:
                fixed_end = max(fixed_end, next_unfinished)
            diagnostics.append("IN_PROGRESS_AND_NEXT_STOP_PROTECTED")
        elif protect_next and minute >= shift_start_minute:
            first_unfinished = next(
                (
                    index
                    for index, item in enumerate(ordered)
                    if item["status"] != "COMPLETED"
                ),
                None,
            )
            if first_unfinished is not None:
                fixed_end = max(fixed_end, first_unfinished)
                diagnostics.append("FIRST_UNFINISHED_PROTECTED_AFTER_SHIFT_START")

        fixed = tuple(ordered[: fixed_end + 1])
        mutable = tuple(
            item for item in ordered[fixed_end + 1 :] if item["status"] == "NEW"
        )
        boundary = (
            fixed[-1]
            if fixed
            else (ordered[max(completed_indexes)] if completed_indexes else None)
        )
        known_finish = boundary.get("planned_finish") if boundary else None
        if isinstance(known_finish, str):
            known_finish = _datetime(known_finish)
        if boundary is not None and boundary["status"] == "COMPLETED":
            actual_finish = boundary.get("actual_completed_at")
            if actual_finish is not None:
                known_finish = _datetime(actual_finish)
                diagnostics.append("ACTUAL_COMPLETION_TIME_USED")
            else:
                diagnostics.append("ACTUAL_COMPLETION_TIME_MISSING")
        boundary_time = max(
            value for value in (snapshot_time, known_finish) if value is not None
        )
        if boundary is not None and boundary["status"] == "IN_PROGRESS":
            diagnostics.append("PLANNED_FINISH_USED_AS_PROGRESS_ESTIMATE")
        return ProtectedRoute(
            engineer_id=engineer_id,
            fixed=fixed,
            mutable=mutable,
            boundary=boundary,
            boundary_time=boundary_time,
            diagnostics=tuple(diagnostics),
        )


def _datetime(value: datetime | str) -> datetime:
    return (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
