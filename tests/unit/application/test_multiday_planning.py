from datetime import date, datetime, timezone

from planning.application.services.future_opportunities import (
    future_opportunity,
    permanent_issue,
    priority_group,
)
from planning.application.services.multi_day_planning import (
    _enforce_sla_hierarchy,
    _order_for_daily_limit,
)
from planning.domain.entities.coordinate import Coordinate
from planning.domain.entities.engineer import Engineer
from planning.domain.entities.job import Job
from planning.domain.entities.planning import PlanningConfig, PlanningInput
from planning.domain.enums import TransportType


def config(**overrides) -> PlanningConfig:
    values = {
        "id": 1,
        "version": 1,
        "sla_overdue_base": 10_000,
        "sla_overdue_per_day": 1_000,
        "sla_today": 9_000,
        "sla_tomorrow": 4_000,
        "sla_2_3_days": 2_000,
        "sla_later": 500,
        "skill_one_engineer": 500,
        "skill_two_engineers": 250,
        "equipment_one_unit": 500,
        "equipment_two_units": 250,
        "window_30": 300,
        "window_60": 150,
        "window_120": 50,
        "travel_cost_per_minute": 1,
        "solver_time_limit_sec": 1,
        "max_jobs_per_run": 1000,
        "travel_provider": "STATIC_TEST",
    }
    values.update(overrides)
    return PlanningConfig(**values)


def snapshot() -> dict:
    return {
        "engineers": [
            {
                "id": 7,
                "transport_type": "CAR",
                "start_address": "base",
                "start_latitude": "58.0",
                "start_longitude": "56.0",
            }
        ],
        "schedules": [
            {
                "engineer_id": 7,
                "work_date": "2026-09-14",
                "shift_start": "08:00:00",
                "shift_end": "18:00:00",
            },
            {
                "engineer_id": 7,
                "work_date": "2026-09-15",
                "shift_start": "08:00:00",
                "shift_end": "18:00:00",
            },
        ],
        "engineer_qualifications": {"7": [3]},
        "required_qualifications": {"9": [3]},
        "required_equipment": {"9": [4]},
        "equipment_units": {"4": 1},
    }


def raw_job(**overrides) -> dict:
    value = {
        "id": 11,
        "work_type_id": 9,
        "service_duration_min": 60,
        "default_service_duration_min": 60,
        "required_transport": "CAR",
        "time_window_start": "09:00:00",
        "time_window_end": "16:00:00",
    }
    value.update(overrides)
    return value


def test_future_opportunity_counts_engineer_day_pairs_and_maps_bonus() -> None:
    value = future_opportunity(
        raw_job(), date(2026, 9, 14), date(2026, 9, 20), snapshot(), config()
    )

    assert value.count == 2
    assert value.rank == 2
    assert value.bonus == 500


def test_permanent_issue_distinguishes_zero_equipment_and_oversized_duration() -> None:
    no_equipment = snapshot()
    no_equipment["equipment_units"] = {"4": 0}
    assert (
        permanent_issue(raw_job(), no_equipment, date(2026, 9, 14), date(2026, 10, 13))[
            0
        ]
        == "EQUIPMENT_UNAVAILABLE_IN_HORIZON"
    )
    assert (
        permanent_issue(
            raw_job(service_duration_min=700),
            snapshot(),
            date(2026, 9, 14),
            date(2026, 10, 13),
        )[0]
        == "DURATION_EXCEEDS_ALL_SHIFTS"
    )


def test_priority_groups_are_relative_to_day_and_current_block() -> None:
    planning_date = date(2026, 9, 14)
    block_end = date(2026, 9, 20)
    assert priority_group(date(2026, 9, 13), planning_date, block_end) == "OVERDUE"
    assert priority_group(planning_date, planning_date, block_end) == "DUE_TODAY"
    assert priority_group(date(2026, 9, 21), planning_date, block_end) == "RESERVE"


def test_primary_penalty_is_greater_than_all_reserve_penalties_and_travel() -> None:
    engineer = Engineer(
        id=1,
        transport_type=TransportType.CAR,
        coordinate=Coordinate(58.0, 56.0),
        shift_start_min=480,
        shift_end_min=1080,
        qualifications=frozenset(),
    )
    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    jobs = [
        Job(
            id=1,
            sla_date=date(2026, 9, 14),
            duration_min=60,
            coordinate=Coordinate(58.1, 56.1),
            window_start_min=0,
            window_end_min=1439,
            required_transport=None,
            required_qualifications=frozenset(),
            required_equipment=frozenset(),
            created_at=now,
            drop_penalty=9_500,
        ),
        Job(
            id=2,
            sla_date=date(2026, 9, 25),
            duration_min=60,
            coordinate=Coordinate(58.2, 56.2),
            window_start_min=0,
            window_end_min=1439,
            required_transport=None,
            required_qualifications=frozenset(),
            required_equipment=frozenset(),
            created_at=now,
            drop_penalty=1_250,
        ),
    ]
    data = PlanningInput(
        project_id=1,
        planning_date=date(2026, 9, 14),
        timezone="UTC",
        config=config(),
        jobs=jobs,
        engineers=[engineer],
        equipment_units={},
        pre_unassigned=[],
        input_jobs_count=2,
        sla_critical_job_ids=frozenset({1}),
        snapshot={},
    )
    decisions = {
        1: {"priority_group": "DUE_TODAY", "cascade_drop_penalty": 9_500},
        2: {"priority_group": "RESERVE", "cascade_drop_penalty": 1_250},
    }

    ordered = _order_for_daily_limit(jobs, decisions)
    ordered = _enforce_sla_hierarchy(ordered, decisions, data)

    assert ordered[0].id == 1
    assert ordered[0].drop_penalty > ordered[1].drop_penalty + 2880
