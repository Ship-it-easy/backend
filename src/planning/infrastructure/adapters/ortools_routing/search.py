"""Общий бюджет времени, параметры поиска и интерпретация статуса OR-Tools."""

import time as monotonic_time

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from planning.application.errors import SolverNoFeasibleSolution, SolverTimeLimit
from planning.domain.entities.planning import PlanningInput, PlanningResult


def search(
    routing: pywrapcp.RoutingModel,
    data: PlanningInput,
    solve_started: float,
    *,
    time_fraction: float = 1.0,
    initial_routes: list[list[int]] | None = None,
) -> tuple[pywrapcp.Assignment, int]:
    """Построить первый вариант и улучшать его до исчерпания прежнего бюджета."""
    parameters = pywrapcp.DefaultRoutingSearchParameters()
    parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    )
    parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    # Полная проверка Time во время поиска учитывает связанные границы
    # всех остановок: отдельно допустимых окон недостаточно для всего маршрута.
    parameters.use_full_propagation = True
    remaining_ms = int(remaining_search_ms(data, solve_started) * time_fraction)
    if remaining_ms <= 0:
        raise SolverTimeLimit("SOLVER_TIME_LIMIT")
    parameters.time_limit.FromMilliseconds(remaining_ms)
    routing.solver().ReSeed(data.config.solver_seed)
    if initial_routes is None:
        assignment = routing.SolveWithParameters(parameters)
    else:
        routing.CloseModelWithParameters(parameters)
        initial = routing.ReadAssignmentFromRoutes(initial_routes, True)
        remaining_ms = min(
            remaining_search_ms(data, solve_started),
            max(1, remaining_ms),
        )
        if remaining_ms <= 0:
            raise SolverTimeLimit("SOLVER_TIME_LIMIT")
        if initial is None:
            raise RuntimeError("INITIAL_ROUTE_RESTORE_FAILED")
        parameters.time_limit.FromMilliseconds(remaining_ms)
        assignment = routing.SolveFromAssignmentWithParameters(initial, parameters)
        if assignment is None and routing.status() == (
            routing_enums_pb2.RoutingSearchStatus.Value.ROUTING_FAIL_TIMEOUT
        ):
            assignment = initial
    solver_time_ms = int((monotonic_time.perf_counter() - solve_started) * 1000)
    search_status = routing_enums_pb2.RoutingSearchStatus.Value
    if assignment is None:
        status = routing.status()
        if status == search_status.ROUTING_FAIL_TIMEOUT:
            raise SolverTimeLimit("SOLVER_TIME_LIMIT")
        if status in (search_status.ROUTING_FAIL, search_status.ROUTING_INFEASIBLE):
            raise SolverNoFeasibleSolution("NO_FEASIBLE_ROUTE")
        raise RuntimeError(f"INVALID_ROUTING_MODEL: status={status}")
    return assignment, solver_time_ms


def remaining_search_ms(data: PlanningInput, solve_started: float) -> int:
    """Общий бюджет построения и поиска; дедлайн события не продлеваем."""
    remaining_ms = int(
        (
            data.config.solver_time_limit_sec
            - (monotonic_time.perf_counter() - solve_started)
        )
        * 1000
    )
    if data.solve_deadline_monotonic is not None:
        remaining_ms = min(
            remaining_ms,
            int((data.solve_deadline_monotonic - monotonic_time.monotonic()) * 1000),
        )
    return remaining_ms


def set_solver_status(
    result: PlanningResult,
    routing: pywrapcp.RoutingModel,
    data: PlanningInput,
    solver_time_ms: int,
) -> None:
    """Различать найденный допустимый план, лимит времени и доказанный оптимум."""
    routing_status = routing.status()
    search_status = routing_enums_pb2.RoutingSearchStatus.Value
    if routing_status == (
        search_status.ROUTING_PARTIAL_SUCCESS_LOCAL_OPTIMUM_NOT_REACHED
    ) or (
        routing_status != search_status.ROUTING_OPTIMAL
        and solver_time_ms >= data.config.solver_time_limit_sec * 1000 - 20
    ):
        result.solver_status = "FEASIBLE_TIME_LIMIT"
    elif routing_status == search_status.ROUTING_OPTIMAL:
        result.solver_status = "OPTIMAL"
    else:
        result.solver_status = "FEASIBLE"
