"""Проверяем бизнес-нарушения в результате, а не вызовы внутренних методов."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from planning.application.validators.planning_result import PlanningValidator
from planning.domain.entities.job import UnassignedJob
from planning.domain.enums import ReasonCode, TransportType
from planning.infrastructure.adapters.planning_solver_ortools import (
    OrToolsPlanningSolver,
)
from tests.unit.infrastructure.test_routing_contract import DemoProvider, demo_input
from tests.unit.infrastructure.test_routing_regressions import Provider, data, job


@pytest.fixture(scope="module")
def valid_example():
    planning = demo_input()
    result = asyncio.run(OrToolsPlanningSolver(DemoProvider()).solve(planning))
    assert PlanningValidator().validate(planning, result) == []
    return planning, result


@pytest.fixture
def example(valid_example):
    return deepcopy(valid_example)


def test_valid_result_is_accepted_without_mutation_and_validator_can_be_reused(example):
    planning, result = example
    before = deepcopy(example)
    validator = PlanningValidator()
    assert validator.validate(planning, result) == []
    assert example == before
    damaged = deepcopy(result)
    damaged.routes[0].jobs[0].sequence = 42
    assert validator.validate(planning, damaged)
    assert validator.validate(planning, result) == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sequence", 9, "invalid sequence for job 1"),
        ("travel_from_previous_min", 9, "job 1 has broken travel sequence"),
        ("travel_from_previous_min", 9, "job 1 has invalid travel time"),
        ("travel_from_previous_min", -1, "job 1 has negative route metrics"),
        ("waiting_before_job_min", 5, "job 1 has invalid waiting time"),
        ("waiting_before_job_min", -1, "job 1 has negative route metrics"),
        ("distance_from_previous_meters", 1, "job 1 has invalid distance"),
        ("distance_from_previous_meters", -1, "job 1 has negative route metrics"),
    ],
)
def test_rejects_incorrect_visit_values(example, field, value, message):
    planning, result = example
    setattr(result.routes[0].jobs[0], field, value)
    assert message in PlanningValidator().validate(planning, result)


@pytest.mark.parametrize(
    ("field", "minutes", "message"),
    [
        ("planned_arrival", 1, "job 1 has broken travel sequence"),
        ("planned_arrival", 1, "job 1 starts before arrival"),
        ("planned_start", -20, "job 1 starts outside its window"),
        ("planned_start", -20, "job 1 is outside engineer shift"),
        ("planned_start", 60, "job 1 starts outside its window"),
        ("planned_finish", 1, "job 1 has invalid service duration"),
        ("planned_finish", 300, "job 1 is outside engineer shift"),
        ("planned_start", 1440, "job 1 is planned on another date"),
    ],
)
def test_rejects_incorrect_visit_times(example, field, minutes, message):
    planning, result = example
    visit = result.routes[0].jobs[0]
    setattr(visit, field, getattr(visit, field) + timedelta(minutes=minutes))
    assert message in PlanningValidator().validate(planning, result)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("total_travel_min", "route 1 has invalid travel total"),
        ("total_service_min", "route 1 has invalid service total"),
        ("total_waiting_min", "route 1 has invalid waiting total"),
        ("total_distance_meters", "route 1 has invalid distance total"),
    ],
)
def test_recomputes_route_totals(example, field, message):
    planning, result = example
    setattr(result.routes[0], field, getattr(result.routes[0], field) + 1)
    assert message in PlanningValidator().validate(planning, result)


def test_checks_route_boundaries_and_equipment(example):
    planning, result = example
    route = result.routes[0]
    route.planned_start -= timedelta(minutes=1)
    route.planned_finish += timedelta(minutes=1)
    route.equipment_type_ids.clear()
    errors = PlanningValidator().validate(planning, result)
    assert "route 1 is outside engineer shift" in errors
    assert "route 1 has invalid finish" in errors
    assert "route 1 has invalid equipment assignment" in errors


def test_detects_duplicate_jobs_routes_and_overlap_in_original_order(example):
    planning, result = example
    result.routes.append(deepcopy(result.routes[0]))
    result.unassigned.append(UnassignedJob(1, 1, ReasonCode.NOT_SELECTED_BY_OPTIMIZER))
    assert PlanningValidator().validate(planning, result)[:3] == [
        "jobs assigned more than once: [1, 2]",
        "jobs both assigned and unassigned: [1]",
        "engineers have multiple routes: [1]",
    ]


def test_missing_and_unknown_jobs_are_detected(example):
    planning, result = example
    result.routes[0].jobs[0].job_id = 999
    errors = PlanningValidator().validate(planning, result)
    assert errors[0] == "assigned and unassigned jobs do not exactly cover the input"
    assert "unknown or ineligible job 999" in errors


def test_pre_unassigned_jobs_are_part_of_required_coverage(example):
    planning, result = example
    planning.pre_unassigned.append(UnassignedJob(99, 500, ReasonCode.DATASET_LIMIT))
    assert "assigned and unassigned jobs do not exactly cover the input" in (
        PlanningValidator().validate(planning, result)
    )
    result.unassigned.append(deepcopy(planning.pre_unassigned[0]))
    result.drop_cost += 500
    assert PlanningValidator().validate(planning, result) == []
    result.unassigned.append(deepcopy(planning.pre_unassigned[0]))
    assert "jobs unassigned more than once: [99]" in (
        PlanningValidator().validate(planning, result)
    )


def test_unknown_engineer_is_reported(example):
    planning, result = example
    result.routes[0].engineer_id = 999
    assert "unknown engineer 999" in PlanningValidator().validate(planning, result)


def test_empty_route_is_reported(example):
    planning, result = example
    result.routes[0].jobs.clear()
    assert "route 1 is empty" in PlanningValidator().validate(planning, result)


@pytest.mark.parametrize("requirement", ["qualification", "transport", "owner"])
def test_checks_all_engineer_compatibility_requirements(example, requirement):
    planning, result = example
    if requirement == "qualification":
        planning.engineers[0] = replace(
            planning.engineers[0], qualifications=frozenset()
        )
    elif requirement == "transport":
        planning.engineers[0] = replace(
            planning.engineers[0], transport_type=TransportType.NONE
        )
    else:
        planning.jobs[0] = replace(
            planning.jobs[0], allowed_engineer_ids=frozenset({2})
        )
    assert "job 1 is incompatible with engineer" in (
        PlanningValidator().validate(planning, result)
    )


def test_equipment_inventory_is_checked_across_engineers(example):
    planning, result = example
    planning.jobs[2] = replace(planning.jobs[2], required_equipment=frozenset({1}))
    result.routes[1].equipment_type_ids.add(1)
    assert "equipment 1 capacity exceeded" in (
        PlanningValidator().validate(planning, result)
    )


def test_preallocated_equipment_is_not_counted_twice_for_same_engineer(example):
    planning, result = example
    planning.preallocated_equipment_by_engineer = {1: frozenset({1})}
    assert PlanningValidator().validate(planning, result) == []
    planning.preallocated_equipment_by_engineer = {99: frozenset({1})}
    assert "equipment 1 capacity exceeded" in (
        PlanningValidator().validate(planning, result)
    )


@pytest.mark.parametrize(
    ("field", "name"),
    [
        ("travel_matrices", "minutes"),
        ("travel_time_seconds_matrices", "seconds"),
        ("distance_matrices", "distance"),
    ],
)
@pytest.mark.parametrize("damage", ["missing", "short", "negative", "float", "bool"])
def test_matrix_errors_stop_validation_before_route_access(
    example, field, name, damage
):
    planning, result = example
    matrices = getattr(result, field)
    message = f"invalid {name} matrix value for auto"
    if damage == "missing":
        del matrices["auto"]
        message = f"missing or invalid {name} matrix for auto"
    elif damage == "short":
        matrices["auto"][0].pop()
        message = f"missing or invalid {name} matrix for auto"
    else:
        matrices["auto"][0][0] = {"negative": -1, "float": 1.5, "bool": True}[damage]
    # Без раннего выхода этот повреждённый metadata вызвал бы KeyError.
    result.objective_metrics = {"unexpected": 1}
    assert PlanningValidator().validate(planning, result) == [message]


def test_selected_arc_must_have_all_three_road_values(example):
    planning, result = example
    for field in (
        "travel_matrices",
        "travel_time_seconds_matrices",
        "distance_matrices",
    ):
        getattr(result, field)["auto"][3][0] = None
    errors = PlanningValidator().validate(planning, result)
    assert "job 1 has invalid travel time" in errors
    assert "job 1 has invalid distance" in errors
    assert "job 1 has no raw travel time" in errors


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("emax", 0, "actual used engineers exceed Emax"),
        ("dmax", 0, "actual total distance exceeds Dmax"),
        ("mmax", 0, "actual maximum distance exceeds Mmax"),
        ("tmax", 0, "actual travel time exceeds Tmax"),
        ("pmax", -1, "actual drop cost exceeds Pmax"),
        ("fixed_active_engineer_count", 1, "invalid fixed active engineer count"),
        ("newly_activated_engineer_count", 0, "invalid newly activated engineer count"),
        ("used_engineer_count", 0, "invalid used engineer count"),
        ("vehicle_fixed_costs", {}, "invalid vehicle fixed costs"),
        ("maximum_objective", 0, "actual objective exceeds proven maximum"),
    ],
)
def test_checks_objective_bounds_and_engineer_counts(example, key, value, message):
    planning, result = example
    result.objective_metrics[key] = value
    assert message in PlanningValidator().validate(planning, result)


@pytest.mark.parametrize("field", ["drop_cost", "travel_cost", "objective"])
def test_checks_final_costs(example, field):
    planning, result = example
    setattr(result, field, getattr(result, field) + 1)
    message = {
        "drop_cost": "invalid drop cost",
        "travel_cost": "invalid travel cost",
        "objective": "invalid lexicographic objective:",
    }[field]
    assert any(
        message in error for error in PlanningValidator().validate(planning, result)
    )


def test_legacy_result_without_objective_metrics_uses_old_cost_formula(example):
    planning, result = example
    result.objective_metrics.clear()
    result.objective = result.drop_cost + result.travel_cost
    assert PlanningValidator().validate(planning, result) == []
    result.objective += 1
    assert PlanningValidator().validate(planning, result) == ["invalid objective"]


async def test_dropped_mandatory_job_is_rejected():
    planning = data([job(1, duration_min=1440)])
    result = await OrToolsPlanningSolver(Provider()).solve(planning)
    assert PlanningValidator().validate(planning, result) == []
    planning.jobs[0] = replace(planning.jobs[0], mandatory=True)
    assert "mandatory job is not assigned" in PlanningValidator().validate(
        planning, result
    )


def test_partial_objective_metadata_keeps_existing_exception_contract(example):
    planning, result = example
    result.objective_metrics = {"unexpected": 1}
    with pytest.raises(KeyError, match="weights"):
        PlanningValidator().validate(planning, result)
