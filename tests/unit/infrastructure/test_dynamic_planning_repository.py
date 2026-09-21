from datetime import date, datetime, timezone
from types import SimpleNamespace

from planning.infrastructure.adapters.dynamic_planning_repository_sqla import (
    _historical_day_result_matches_assignments,
    _same_instant,
)


def test_same_instant_accepts_equivalent_utc_serializations() -> None:
    assert _same_instant(
        "2026-09-19T14:16:07.246984Z",
        datetime(2026, 9, 19, 14, 16, 7, 246984, tzinfo=timezone.utc),
    )


def test_same_instant_detects_a_real_update() -> None:
    assert not _same_instant(
        "2026-09-19T14:16:07.246984Z",
        "2026-09-19T14:16:08.246984+00:00",
    )


def test_historical_day_result_rejects_moved_or_removed_route_jobs() -> None:
    result = SimpleNamespace(planning_date=date(2026, 9, 21))
    route = [{"job_id": 10, "engineer_id": 2}]

    assert not _historical_day_result_matches_assignments(
        result,
        route,
        {10: {"planning_date": date(2026, 9, 20), "engineer_id": 2}},
    )
    assert not _historical_day_result_matches_assignments(result, route, {})


def test_historical_day_result_keeps_compatible_and_unassigned_results() -> None:
    result = SimpleNamespace(planning_date=date(2026, 9, 21))
    route = [{"job_id": 10, "engineer_id": 2}]

    assert _historical_day_result_matches_assignments(
        result,
        route,
        {10: {"planning_date": "2026-09-21", "engineer_id": 2}},
    )
    assert _historical_day_result_matches_assignments(result, [], {})


def test_board_coordinate_reads_normalized_and_flat_snapshots():
    from planning.infrastructure.adapters.dynamic_planning_repository_sqla import (
        _coordinate,
    )

    coordinate = {"latitude": 58.01, "longitude": 56.24}
    assert _coordinate({"coordinate": coordinate}) == coordinate
    assert _coordinate(coordinate) == coordinate
    assert (
        _coordinate({"latitude": None, "longitude": None, "coordinate": coordinate})
        == coordinate
    )
    assert _coordinate({}) is None
