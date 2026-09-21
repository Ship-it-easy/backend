"""Последовательные цели без произведения всех уровней в одном int64."""

import time as monotonic_time

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from planning.application.errors import SolverTimeLimit
from planning.domain.entities.planning import PlanningInput, PlanningResult

from .model import DailyRoutingModel
from .result import extract_result
from .search import remaining_search_ms, search
from .travel import RoutingMatrices


def solve_lexicographically(
    data: PlanningInput,
    travel: RoutingMatrices,
    first_model: DailyRoutingModel,
    solve_started: float,
) -> PlanningResult:
    """Решить бизнес-цели по порядку, фиксируя значение каждой предыдущей."""
    drop_names = [str(stage["name"]) for stage in first_model.drop_priority_stages]
    stage_names = [*drop_names, "ROUTE"]
    locked: dict[str, int] = {}
    records: list[dict[str, int | str | bool]] = []
    model = first_model
    initial_routes: list[list[int]] | None = None
    result: PlanningResult | None = None

    for position, name in enumerate(stage_names):
        if position:
            if remaining_search_ms(data, solve_started) <= 0:
                break
            model = DailyRoutingModel(
                data,
                travel,
                objective_stage=name,
                locked_drop_objectives=locked,
            )
            model.build()
        remaining_stages = len(stage_names) - position
        try:
            assignment, total_time_ms = search(
                model.routing,
                data,
                solve_started,
                time_fraction=1 / remaining_stages,
                initial_routes=initial_routes,
            )
        except SolverTimeLimit:
            break
        stage_objective = int(assignment.ObjectiveValue())
        elapsed_before = sum(int(item["elapsed_ms"]) for item in records)
        records.append(
            {
                "name": name,
                "objective": stage_objective,
                "routing_status": model.routing.status(),
                "proven_optimal": model.routing.status()
                == routing_enums_pb2.RoutingSearchStatus.Value.ROUTING_OPTIMAL,
                "elapsed_ms": max(0, total_time_ms - elapsed_before),
            }
        )
        result = extract_result(data, travel, model, assignment)
        initial_routes = _route_nodes(model, assignment)
        if name != "ROUTE":
            locked[name] = stage_objective

    if result is None:
        raise SolverTimeLimit("SOLVER_TIME_LIMIT")
    result.solver_time_ms = int((monotonic_time.perf_counter() - solve_started) * 1000)
    result.objective_metrics["stages"] = records
    result.objective_metrics["locked_drop_objectives"] = locked
    complete = [item["name"] for item in records] == stage_names
    result.objective_metrics["all_stages_completed"] = complete
    result.solver_status = (
        "OPTIMAL"
        if complete and all(bool(item["proven_optimal"]) for item in records)
        else "FEASIBLE_TIME_LIMIT"
    )
    return result


def _route_nodes(
    model: DailyRoutingModel, assignment: pywrapcp.Assignment
) -> list[list[int]]:
    routes = []
    for vehicle in range(model.vehicle_count):
        nodes = []
        index = assignment.Value(model.routing.NextVar(model.routing.Start(vehicle)))
        while not model.routing.IsEnd(index):
            nodes.append(model.manager.IndexToNode(index))
            index = assignment.Value(model.routing.NextVar(index))
        routes.append(nodes)
    return routes
