from datetime import date

from planning.application.services.planning_board import (
    REASON_TEXTS,
    assigned_primary_reason,
    reason,
    unassigned_reason,
    unassigned_sort_key,
)


def test_assigned_reason_is_work_priority_then_overdue_then_due_today() -> None:
    planning_date = date(2026, 9, 19)

    assert (
        assigned_primary_reason(
            {
                "priority": "CRITICAL",
                "sla_date": "2026-09-18",
                "priority_bonus": 3_000_000,
            },
            planning_date,
            2,
        )["code"]
        == "WORK_TYPE_PRIORITY"
    )
    assert (
        assigned_primary_reason(
            {"priority": "LOW", "sla_date": "2026-09-17"},
            planning_date,
            2,
        )["parameters"]["overdue_days"]
        == 2
    )
    assert (
        assigned_primary_reason(
            {"priority": "LOW", "sla_date": "2026-09-19"},
            planning_date,
            2,
        )["code"]
        == "SLA_DUE_TODAY"
    )


def test_unassigned_reason_maps_saved_legacy_codes_without_guessing() -> None:
    value = unassigned_reason(
        "NO_COMPATIBLE_ENGINEER_IN_HORIZON",
        solver_status="SUCCESS",
        diagnostic_flags={"qualification_ids": [7]},
    )

    assert value == {
        "code": "NO_ELIGIBLE_ENGINEER",
        "text": "Нет инженера, удовлетворяющего обязательным требованиям",
        "parameters": {"qualification_ids": [7]},
        "known": True,
    }


def test_time_limit_does_not_replace_saved_job_reason() -> None:
    assert (
        unassigned_reason(
            "NOT_SELECTED_BY_OPTIMIZER", solver_status="FEASIBLE_TIME_LIMIT"
        )["code"]
        == "DROPPED_BY_OBJECTIVE"
    )


def test_equipment_reason_names_the_missing_catalog_item_and_quantity() -> None:
    value = unassigned_reason(
        "EQUIPMENT_UNAVAILABLE_IN_HORIZON",
        solver_status="SUCCESS",
        diagnostic_flags={
            "missing_equipment": [{"id": 4, "name": "Лестница", "available_units": 0}]
        },
    )

    assert value["code"] == "NO_EQUIPMENT"
    assert value["text"] == (
        "Недоступно обязательное оборудование: Лестница (доступно: 0)"
    )


def test_engineer_unavailability_is_not_hidden_as_a_generic_shift_problem() -> None:
    assert reason("ENGINEER_UNAVAILABLE")["text"] == (
        "Назначенный инженер стал недоступен"
    )


def test_horizon_has_explicit_outcome() -> None:
    assert (
        unassigned_reason(
            "NOT_ASSIGNED_WITHIN_HORIZON",
            solver_status="SUCCESS",
            final_horizon=True,
        )["code"]
        == "HORIZON_EXHAUSTED"
    )


def test_unknown_reason_has_safe_text_and_marker() -> None:
    assert reason("NEW_SERVER_REASON") == {
        "code": "NEW_SERVER_REASON",
        "text": "Подробная причина недоступна",
        "parameters": {},
        "known": False,
    }


def test_every_public_reason_code_has_human_readable_text() -> None:
    for code, text in REASON_TEXTS.items():
        value = reason(code)
        assert value["known"] is True
        assert value["code"] == code
        assert value["text"] == text
        assert value["text"] != "Подробная причина недоступна"


def test_unassigned_sort_is_overdue_then_priority_then_sla_then_created() -> None:
    planning_date = date(2026, 9, 19)
    jobs = [
        {
            "job_id": 1,
            "sla_date": "2026-09-20",
            "priority": "CRITICAL",
            "created_at": "2026-09-01T10:00:00Z",
        },
        {
            "job_id": 2,
            "sla_date": "2026-09-18",
            "priority": "LOW",
            "created_at": "2026-09-02T10:00:00Z",
        },
        {
            "job_id": 3,
            "sla_date": "2026-09-20",
            "priority": "LOW",
            "created_at": "2026-09-01T10:00:00Z",
        },
    ]

    assert [
        item["job_id"]
        for item in sorted(
            jobs, key=lambda item: unassigned_sort_key(item, planning_date)
        )
    ] == [2, 1, 3]
