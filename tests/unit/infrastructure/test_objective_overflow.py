"""Общая оценка делится на точные бизнес-этапы; небезопасный вход отклоняется."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from planning.application.errors import SolverTimeLimit
from planning.application.interfaces.travel_matrix_provider import TravelMatrix
from planning.application.services.multi_day_planning import _prepare_sla_hierarchy
from planning.application.validators.planning_result import PlanningValidator
from planning.domain.enums import TransportType
from planning.infrastructure.adapters import planning_solver_ortools
from planning.infrastructure.adapters.ortools_routing import lexicographic
from planning.infrastructure.adapters.ortools_routing.objective import (
    calculate_objective_ranges,
)
from planning.infrastructure.adapters.planning_solver_ortools import (
    OrToolsPlanningSolver,
)
from tests.unit.infrastructure.test_routing_regressions import data, engineer, job


class SavedMatrixProvider:
    def __init__(self, matrices):
        self.matrices = matrices

    async def get_matrix(self, coordinates, profile, cache_ttl_days=None):
        values = self.matrices[profile]
        return TravelMatrix(values["seconds"], values["meters"], profile, "REGRESSION")


@pytest.fixture
def overflow_input():
    fixture = json.loads(
        (
            Path(__file__).parents[2] / "fixtures/planning_objective_overflow.json"
        ).read_text()
    )
    jobs = [
        job(
            index + 1,
            **{
                **row,
                "required_qualifications": frozenset(row["required_qualifications"]),
                "required_equipment": frozenset(row["required_equipment"]),
                "required_transport": (
                    TransportType(row["required_transport"])
                    if row["required_transport"]
                    else None
                ),
            },
        )
        for index, row in enumerate(fixture["jobs"])
    ]
    engineers = [
        engineer(
            index + 1,
            **{
                **row,
                "transport_type": TransportType(row["transport_type"]),
                "qualifications": frozenset(row["qualifications"]),
            },
        )
        for index, row in enumerate(fixture["engineers"])
    ]
    planning = data(
        jobs,
        engineers,
        equipment_units={int(k): v for k, v in fixture["equipment_units"].items()},
    )
    return planning, SavedMatrixProvider(fixture["matrices"])


async def test_saved_overflow_stops_before_search(monkeypatch, overflow_input):
    planning, provider = overflow_input
    search = Mock(side_effect=AssertionError("Search must not start after overflow"))
    monkeypatch.setattr(planning_solver_ortools, "_search", search)
    original_penalties = [item.drop_penalty for item in planning.jobs]
    with pytest.raises(RuntimeError, match="^OBJECTIVE_RANGE_OVERFLOW$"):
        await OrToolsPlanningSolver(provider).solve(planning)
    search.assert_not_called()
    assert planning.config.distance_unit_meters == 10
    assert planning.config.time_unit_seconds == 60
    assert [item.drop_penalty for item in planning.jobs] == original_penalties


async def test_saved_overflow_uses_business_stages_when_prepared(overflow_input):
    planning, provider = overflow_input
    groups = [
        "OVERDUE",
        "OVERDUE",
        "OVERDUE",
        "OVERDUE",
        "DUE_TODAY",
        "DUE_TODAY",
        "DUE_IN_1_DAY",
        "DUE_IN_1_DAY",
        "DUE_IN_2_3_DAYS",
        "DUE_IN_2_3_DAYS",
        "DUE_IN_2_3_DAYS",
        "DUE_LATER_IN_CURRENT_BLOCK",
        "DUE_LATER_IN_CURRENT_BLOCK",
        "DUE_LATER_IN_CURRENT_BLOCK",
        "DUE_LATER_IN_CURRENT_BLOCK",
        "DUE_LATER_IN_CURRENT_BLOCK",
    ]
    decisions = {
        item.id: {"priority_group": group}
        for item, group in zip(planning.jobs, groups, strict=True)
    }
    prepared, stages, encoding = _prepare_sla_hierarchy(
        planning.jobs, decisions, planning
    )
    planning = replace(
        planning,
        jobs=prepared,
        config=replace(planning.config, solver_time_limit_sec=3),
        snapshot={
            **planning.snapshot,
            "drop_priority_stages": stages,
            "penalty_encoding": encoding,
        },
    )
    result = await OrToolsPlanningSolver(provider).solve(planning)
    assert result.objective_metrics["solve_strategy"] == "LEXICOGRAPHIC_STAGES"
    assert result.objective_metrics["all_stages_completed"] is True
    assert result.objective_metrics["objective_stage"] == "ROUTE"
    assert PlanningValidator().validate(planning, result) == []


async def test_one_hundred_jobs_fit_lexicographic_numeric_ranges():
    group_names = [
        "OVERDUE",
        "DUE_TODAY",
        "DUE_IN_1_DAY",
        "DUE_IN_2_3_DAYS",
        "DUE_LATER_IN_CURRENT_BLOCK",
        "RESERVE",
    ]
    jobs = [
        job(
            index + 1,
            duration_min=5,
            window_end_min=1080,
            drop_penalty=1_000 + (index % 7) * 50,
        )
        for index in range(100)
    ]
    planning = data(
        jobs,
        [
            engineer(1, shift_end_min=1080),
            engineer(2, shift_end_min=1080),
            engineer(3, shift_end_min=1080),
        ],
    )
    decisions = {
        item.id: {"priority_group": group_names[index % len(group_names)]}
        for index, item in enumerate(jobs)
    }
    prepared, stages, encoding = _prepare_sla_hierarchy(jobs, decisions, planning)
    planning = replace(
        planning,
        jobs=prepared,
        config=replace(planning.config, solver_time_limit_sec=3),
        snapshot={
            "drop_priority_stages": stages,
            "penalty_encoding": encoding,
        },
    )
    size = len(jobs) + len(planning.engineers)
    seconds = [[0 if a == b else 60 for b in range(size)] for a in range(size)]
    meters = [[0 if a == b else 100 for b in range(size)] for a in range(size)]
    ranges = calculate_objective_ranges(planning, {"auto": seconds}, {"auto": meters})
    assert ranges["solve_strategy"] == "LEXICOGRAPHIC_STAGES"
    assert ranges["nmax"] == 100
    assert ranges["maximum_objective"] < 2**63 - 1
    assert ranges["composite_maximum_objective"] >= 2**63 - 1
    result = await OrToolsPlanningSolver(
        SavedMatrixProvider({"auto": {"seconds": seconds, "meters": meters}})
    ).solve(planning)
    assert result.objective_metrics["all_stages_completed"] is True
    assert sum(len(route.jobs) for route in result.routes) == 100
    assert PlanningValidator().validate(planning, result) == []


async def test_time_limit_keeps_last_valid_business_stage(monkeypatch):
    jobs = [job(1, drop_penalty=1_200), job(2, drop_penalty=900)]
    planning = data(jobs, [engineer(1)])
    decisions = {
        jobs[0].id: {"priority_group": "OVERDUE"},
        jobs[1].id: {"priority_group": "DUE_TODAY"},
    }
    prepared, stages, _encoding = _prepare_sla_hierarchy(jobs, decisions, planning)
    planning = replace(
        planning,
        jobs=prepared,
        snapshot={
            "drop_priority_stages": stages,
            "penalty_encoding": "LEXICOGRAPHIC_STAGES_REQUIRED",
        },
    )
    seconds = [[0 if a == b else 60 for b in range(3)] for a in range(3)]
    meters = [[0 if a == b else 100 for b in range(3)] for a in range(3)]
    original_search = lexicographic.search
    calls = 0

    def stop_after_first_stage(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SolverTimeLimit("SOLVER_TIME_LIMIT")
        return original_search(*args, **kwargs)

    monkeypatch.setattr(lexicographic, "search", stop_after_first_stage)
    result = await OrToolsPlanningSolver(
        SavedMatrixProvider({"auto": {"seconds": seconds, "meters": meters}})
    ).solve(planning)
    assert result.solver_status == "FEASIBLE_TIME_LIMIT"
    assert result.objective_metrics["all_stages_completed"] is False
    assert result.objective_metrics["objective_stage"] == stages[0]["name"]
    assert PlanningValidator().validate(planning, result) == []


async def test_validator_rejects_tampered_stage_contract(overflow_input):
    planning, provider = overflow_input
    decisions = {item.id: {"priority_group": "OVERDUE"} for item in planning.jobs}
    prepared, stages, _encoding = _prepare_sla_hierarchy(
        planning.jobs, decisions, planning
    )
    planning = replace(
        planning,
        jobs=prepared,
        snapshot={
            **planning.snapshot,
            "drop_priority_stages": stages,
            "penalty_encoding": "LEXICOGRAPHIC_STAGES_REQUIRED",
        },
    )
    result = await OrToolsPlanningSolver(provider).solve(planning)
    tampered = deepcopy(result)
    first_stage = tampered.objective_metrics["drop_priority_stages"][0]
    first_stage["maximum_objective"] += 1
    errors = PlanningValidator().validate(planning, tampered)
    assert "invalid drop priority stage definitions" in errors
    assert any(error.startswith("invalid drop priority stage:") for error in errors)

    truncated = deepcopy(result)
    del truncated.objective_metrics["drop_priority_stages"][0]["job_costs"]
    truncated_errors = PlanningValidator().validate(planning, truncated)
    assert "invalid drop priority stage definitions" in truncated_errors
    assert any(
        error.startswith("invalid drop priority stage:") for error in truncated_errors
    )


def test_tampered_stage_contract_stops_before_solver():
    jobs = [job(1, drop_penalty=1_200), job(2, drop_penalty=900)]
    planning = data(jobs)
    decisions = {
        jobs[0].id: {"priority_group": "OVERDUE"},
        jobs[1].id: {"priority_group": "DUE_TODAY"},
    }
    prepared, stages, _encoding = _prepare_sla_hierarchy(jobs, decisions, planning)
    stages[0]["job_costs"] = {}
    planning = replace(
        planning,
        jobs=prepared,
        snapshot={
            "drop_priority_stages": stages,
            "penalty_encoding": "LEXICOGRAPHIC_STAGES_REQUIRED",
        },
    )
    matrices = {"auto": [[0] * 3 for _ in range(3)]}
    with pytest.raises(RuntimeError, match="^OBJECTIVE_RANGE_OVERFLOW$"):
        calculate_objective_ranges(planning, matrices, matrices)


def test_stages_preserve_the_previous_business_order_exhaustively():
    group_names = [
        "OVERDUE",
        "OVERDUE",
        "DUE_TODAY",
        "DUE_TODAY",
        "DUE_IN_1_DAY",
        "DUE_IN_2_3_DAYS",
        "DUE_LATER_IN_CURRENT_BLOCK",
        "RESERVE",
    ]
    jobs = [
        job(index + 1, drop_penalty=value)
        for index, value in enumerate([1200, 1050, 950, 800, 700, 550, 400, 250])
    ]
    jobs[0] = replace(jobs[0], priority="CRITICAL")
    jobs[2] = replace(jobs[2], priority="HIGH")
    planning = data(jobs)
    decisions = {
        item.id: {"priority_group": group}
        for item, group in zip(jobs, group_names, strict=True)
    }
    adjusted, stages, _ = _prepare_sla_hierarchy(jobs, decisions, planning)
    old_penalties = [item.drop_penalty for item in adjusted]
    stage_costs = [
        [int(stage["job_costs"].get(str(item.id), 0)) for item in jobs]
        for stage in stages
    ]
    alternatives = []
    for mask in range(1 << len(jobs)):
        old = sum(
            penalty for index, penalty in enumerate(old_penalties) if mask >> index & 1
        )
        vector = tuple(
            sum(cost for index, cost in enumerate(costs) if mask >> index & 1)
            for costs in stage_costs
        )
        alternatives.append((old, vector))
    for old_a, vector_a in alternatives:
        for old_b, vector_b in alternatives:
            assert (old_a > old_b) - (old_a < old_b) == (
                (vector_a > vector_b) - (vector_a < vector_b)
            )


@pytest.mark.parametrize("at_limit", [False, True])
def test_objective_rejects_int64_boundary(at_limit):
    # Нулевые дороги дают W_drop = 2 и максимум 2 * Pmax + 1.
    # Штраф 1 сохраняет общий делитель 1: проверяем именно числовую границу.
    int64_max = 2**63 - 1
    pmax = (int64_max - 1) // 2 - (not at_limit)
    planning = data([job(1, drop_penalty=1), job(2, drop_penalty=pmax - 1)])
    matrices = {"auto": [[0] * 3 for _ in range(3)]}
    if at_limit:
        with pytest.raises(RuntimeError, match="^OBJECTIVE_RANGE_OVERFLOW$"):
            calculate_objective_ranges(planning, matrices, matrices)
    else:
        metrics = calculate_objective_ranges(planning, matrices, matrices)
        assert metrics["maximum_objective"] == int64_max - 2
        assert metrics["solve_strategy"] == "WEIGHTED_SINGLE_PASS"


def test_exact_common_divisor_preserves_penalty_order():
    planning = data([job(1, drop_penalty=10**30), job(2, drop_penalty=2 * 10**30)])
    matrices = {"auto": [[0] * 3 for _ in range(3)]}
    metrics = calculate_objective_ranges(planning, matrices, matrices)
    assert metrics["drop_cost_divisor"] == 10**30
    assert metrics["pmax"] == 3
    assert metrics["maximum_objective"] == 7
