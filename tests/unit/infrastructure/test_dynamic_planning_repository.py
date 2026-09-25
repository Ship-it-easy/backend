from datetime import date, datetime, timezone
from types import SimpleNamespace

from planning.infrastructure.adapters.dynamic_planning_repository_sqla import (
    _changes,
    _classify_carried_forward_jobs,
    _comparison_reason_detail,
    _historical_day_result_matches_assignments,
    _include_final_unassigned_rows,
    _planning_input_changes,
    _planning_metrics,
    _published_plan_status,
    _resolved_displaced_reason,
    _same_assignment,
    _same_instant,
    _same_unassigned_reason,
    _snapshot_requirement_ids,
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


def test_partial_publication_is_not_hidden_by_solver_quality() -> None:
    assert _published_plan_status("PARTIAL", True) == "PARTIAL"
    assert _published_plan_status("SUCCESS", True) == "SUCCESS"


def test_final_horizon_issue_is_visible_without_a_daily_solver_row() -> None:
    rows = _include_final_unassigned_rows(
        [
            {
                "job_id": 1,
                "primary_reason_code": "NOT_SELECTED_BY_OPTIMIZER",
                "diagnostic_flags": {},
                "drop_penalty": 10,
            }
        ],
        [
            {"job_id": 1, "primary_reason_code": "NO_SHIFT_IN_HORIZON"},
            {
                "job_id": 2,
                "primary_reason_code": "EQUIPMENT_UNAVAILABLE_IN_HORIZON",
                "diagnostic_flags": {"equipment_type_ids": [4]},
            },
        ],
    )

    assert [item["job_id"] for item in rows] == [1, 2]
    assert rows[0]["primary_reason_code"] == "NO_SHIFT_IN_HORIZON"
    assert rows[0]["_final_horizon_outcome"] is True
    assert rows[1]["primary_reason_code"] == "EQUIPMENT_UNAVAILABLE_IN_HORIZON"
    assert rows[1]["_final_horizon_outcome"] is True


def test_snapshot_requirements_fall_back_to_batch_work_type_maps() -> None:
    snapshot = {
        "required_equipment": {"9": [4]},
        "required_qualifications": {"9": [2, 1]},
    }
    job = {"id": 12, "work_type_id": 9}

    assert _snapshot_requirement_ids(job, snapshot, "required_equipment") == [4]
    assert _snapshot_requirement_ids(job, snapshot, "required_qualifications") == [
        1,
        2,
    ]


def test_final_reason_does_not_replace_engineer_unavailability_event() -> None:
    assert (
        _resolved_displaced_reason("ENGINEER_UNAVAILABLE", "NO_SHIFT_IN_HORIZON")
        == "ENGINEER_UNAVAILABLE"
    )
    assert (
        _resolved_displaced_reason(
            "NOT_ASSIGNED_IN_NEW_HORIZON", "EQUIPMENT_UNAVAILABLE_IN_HORIZON"
        )
        == "EQUIPMENT_UNAVAILABLE_IN_HORIZON"
    )


def test_comparison_reason_uses_equipment_name_and_snapshot_quantity() -> None:
    detail = _comparison_reason_detail(
        "EQUIPMENT_UNAVAILABLE_IN_HORIZON",
        {
            "diagnostic_flags": {"equipment_type_ids": [4]},
        },
        12,
        {
            "equipment_units": {"4": 0},
            "equipment_types": [{"id": 4, "name": "Лестница"}],
        },
    )

    assert detail["code"] == "NO_EQUIPMENT"
    assert detail["text"] == (
        "Недоступно обязательное оборудование: Лестница (доступно: 0)"
    )


def test_unassigned_comparison_uses_public_reason_codes() -> None:
    assert _same_unassigned_reason(
        {"primary_reason_code": "DAILY_EQUIPMENT_CAPACITY"},
        {"primary_reason_code": "EQUIPMENT_UNAVAILABLE_IN_HORIZON"},
    )
    assert not _same_unassigned_reason(
        {"primary_reason_code": "NO_SHIFT_IN_HORIZON"},
        {"primary_reason_code": "EQUIPMENT_UNAVAILABLE_IN_HORIZON"},
    )
    assert not _same_unassigned_reason(
        {
            "primary_reason_code": "DAILY_EQUIPMENT_CAPACITY",
            "diagnostic_flags": {"required_equipment_type_ids": [4]},
        },
        {
            "primary_reason_code": "EQUIPMENT_UNAVAILABLE_IN_HORIZON",
            "diagnostic_flags": {"equipment_type_ids": [5]},
        },
    )


def test_assignment_comparison_includes_route_metrics() -> None:
    before = {
        "planning_date": "2026-09-24",
        "engineer_id": 7,
        "sequence": 1,
        "planned_start": "2026-09-24T08:00:00+00:00",
        "planned_finish": "2026-09-24T09:00:00+00:00",
        "travel_from_previous_min": 20,
        "waiting_before_job_min": 0,
        "distance_from_previous_meters": 8000,
        "requirement_snapshot": {},
    }

    assert _same_assignment(before, {**before})
    assert _same_assignment(
        before,
        {
            **before,
            "requirement_snapshot": {
                "virtual_start": None,
                "replan_boundary": None,
                "protection_diagnostics": [],
            },
        },
    )
    assert not _same_assignment(
        before, {**before, "distance_from_previous_meters": 1200}
    )


def test_plan_changes_capture_the_exact_previous_route_job() -> None:
    common = {
        "planning_date": "2026-09-24",
        "engineer_id": 7,
        "planned_start": "2026-09-24T10:00:00+00:00",
        "planned_finish": "2026-09-24T11:00:00+00:00",
        "travel_from_previous_min": 10,
        "waiting_before_job_min": 0,
        "distance_from_previous_meters": 1200,
        "requirement_snapshot": {},
    }
    old = {
        1: {**common, "job_id": 1, "sequence": 1},
        2: {**common, "job_id": 2, "sequence": 2},
    }
    new = {
        3: {**common, "job_id": 3, "sequence": 1},
        2: {**common, "job_id": 2, "sequence": 2},
    }

    changes = _changes(old, new, version_id=5, project_id=1)
    changed_job = next(item for item in changes if item["job_id"] == 2)

    assert changed_job["old_assignment"]["previous_job_id"] == 1
    assert changed_job["new_assignment"]["previous_job_id"] == 3


def test_planning_metrics_keep_daily_staff_and_engineer_distance() -> None:
    metrics = _planning_metrics(
        [
            {
                "job_id": 1,
                "planning_date": "2026-09-24",
                "engineer_id": 7,
                "engineer_name": "Васан",
                "distance_from_previous_meters": 1200,
            },
            {
                "job_id": 2,
                "planning_date": "2026-09-24",
                "engineer_id": 7,
                "engineer_name": "Васан",
                "distance_from_previous_meters": 800,
            },
            {
                "job_id": 3,
                "planning_date": "2026-09-24",
                "engineer_id": 9,
                "engineer_name": "Анна",
                "distance_from_previous_meters": 3500,
            },
        ]
    )

    assert metrics["days"] == [
        {
            "planning_date": "2026-09-24",
            "personnel_count": 2,
            "assigned_jobs_count": 3,
            "distance_meters": 5500,
            "engineers": [
                {
                    "engineer_id": 7,
                    "engineer_name": "Васан",
                    "assigned_jobs_count": 2,
                    "distance_meters": 2000,
                },
                {
                    "engineer_id": 9,
                    "engineer_name": "Анна",
                    "assigned_jobs_count": 1,
                    "distance_meters": 3500,
                },
            ],
        }
    ]
    assert metrics["totals"] == {
        "personnel_count": 2,
        "assigned_jobs_count": 3,
        "distance_meters": 5500,
    }


def test_planning_metrics_include_days_without_assignments() -> None:
    metrics = _planning_metrics([], [date(2026, 9, 24)])

    assert metrics["days"] == [
        {
            "planning_date": "2026-09-24",
            "personnel_count": 0,
            "assigned_jobs_count": 0,
            "distance_meters": 0,
            "engineers": [],
        }
    ]
    assert metrics["totals"] == {
        "personnel_count": 0,
        "assigned_jobs_count": 0,
        "distance_meters": 0,
    }


def test_planning_input_changes_report_concrete_snapshot_differences() -> None:
    previous = {
        "project": {
            "effective_start_date": "2026-09-24",
            "maximum_horizon_end": "2026-09-30",
        },
        "jobs": [{"id": 1, "address": "Старая"}],
        "engineers": [{"id": 7, "name": "Васан"}],
        "schedules": [
            {
                "engineer_id": 7,
                "work_date": "2026-09-24",
                "shift_start": "08:00:00",
                "shift_end": "17:00:00",
            }
        ],
        "equipment_units": {"4": 0},
        "equipment_types": [{"id": 4, "name": "Лестница"}],
        "config": {"travel_weight": 1},
    }
    current = {
        **previous,
        "jobs": [{"id": 1, "address": "Новая"}, {"id": 2}],
        "schedules": [
            {
                "engineer_id": 7,
                "work_date": "2026-09-24",
                "shift_start": "09:00:00",
                "shift_end": "17:00:00",
            }
        ],
        "equipment_units": {"4": 2},
    }

    changes = _planning_input_changes(previous, current)

    assert changes["snapshot_available"] is True
    assert changes["new_job_ids"] == [2]
    assert changes["changed_job_ids"] == [1]
    assert changes["job_changes"] == [
        {
            "job_id": 1,
            "changed_fields": ["address"],
            "changes": [{"field": "address", "before": "Старая", "after": "Новая"}],
        }
    ]
    assert changes["schedule_changes"][0]["engineer_name"] == "Васан"
    assert changes["equipment_changes"] == [
        {
            "equipment_type_id": 4,
            "equipment_name": "Лестница",
            "before_units": 0,
            "after_units": 2,
        }
    ]


def test_horizon_shift_does_not_look_like_schedule_or_requirement_change() -> None:
    previous = {
        "project": {
            "effective_start_date": "2026-09-25",
            "maximum_horizon_end": "2026-10-23",
        },
        "jobs": [{"id": 1, "work_type_id": 9}],
        "engineers": [{"id": 7, "name": "Васан"}],
        "schedules": [
            {"engineer_id": 7, "work_date": "2026-09-25"},
            {"engineer_id": 7, "work_date": "2026-09-26"},
        ],
        "required_equipment": {"9": [4]},
        "required_qualifications": {"9": [2]},
        "engineer_qualifications": {"7": [2]},
    }
    current = {
        "project": {
            "effective_start_date": "2026-09-26",
            "maximum_horizon_end": "2026-10-24",
        },
        "jobs": [],
        "engineers": [{"id": 7, "name": "Васан"}],
        "schedules": [
            {"engineer_id": 7, "work_date": "2026-09-26"},
            {"engineer_id": 7, "work_date": "2026-10-24"},
        ],
        "required_equipment": {},
        "required_qualifications": {},
        "engineer_qualifications": {"7": [2]},
    }

    changes = _planning_input_changes(previous, current)

    assert changes["schedule_changes"] == []
    assert changes["required_equipment_changed_work_type_ids"] == []
    assert changes["required_qualification_changed_work_type_ids"] == []
    assert changes["horizon_changed"] is True
    assert changes["horizon_change"] == {
        "effective_start_date": {
            "before": "2026-09-25",
            "after": "2026-09-26",
        },
        "maximum_horizon_end": {
            "before": "2026-10-23",
            "after": "2026-10-24",
        },
    }


def test_jobs_before_new_horizon_are_carried_forward_not_removed() -> None:
    changes = _classify_carried_forward_jobs(
        {"removed_job_ids": [1, 2]},
        [
            {"job_id": 1, "planning_date": "2026-09-25"},
            {"job_id": 2, "planning_date": "2026-09-27"},
        ],
        {"project": {"effective_start_date": "2026-09-26"}},
    )

    assert changes["carried_forward_job_ids"] == [1]
    assert changes["removed_job_ids"] == [2]


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
