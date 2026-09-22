"""Дневной расчёт маршрутов: дороги → модель → поиск → расписание.

Каскад дней, SLA-приоритеты, защита текущей работы и публикация находятся
в application. Здесь сохраняется публичный адаптер PlanningSolver.
Подробное объяснение: docs/ROUTING_ALGORITHM.md.
"""

import asyncio
import time as monotonic_time

from planning.application.interfaces.travel_matrix_provider import TravelMatrixProvider
from planning.domain.entities.planning import PlanningInput, PlanningResult
from planning.infrastructure.adapters.ortools_routing.lexicographic import (
    solve_lexicographically,
)
from planning.infrastructure.adapters.ortools_routing.model import DailyRoutingModel

# Совместимость с существующими тестами и скриптом аудита.
from planning.infrastructure.adapters.ortools_routing.objective import (
    calculate_objective_ranges as _objective_ranges,
)
from planning.infrastructure.adapters.ortools_routing.result import (
    empty_result,
    extract_result,
)
from planning.infrastructure.adapters.ortools_routing.search import (
    search as _search,
)
from planning.infrastructure.adapters.ortools_routing.search import (
    set_solver_status as _set_solver_status,
)
from planning.infrastructure.adapters.ortools_routing.travel import (
    RoutingMatrices,
    prepare_travel_matrices,
)
from planning.infrastructure.adapters.travel_matrix_provider_factory import (
    TravelMatrixProviderFactory,
)

__all__ = ["OrToolsPlanningSolver", "OrToolsPlanningSolverFactory", "_objective_ranges"]


class OrToolsPlanningSolver:
    def __init__(self, matrix_provider: TravelMatrixProvider):
        self._matrix_provider = matrix_provider

    async def solve(self, data: PlanningInput) -> PlanningResult:
        """Рассчитать один день; вход уже подготовлен прикладными сервисами."""
        if not data.jobs or not data.engineers:
            return empty_result(data)

        travel = await prepare_travel_matrices(data, self._matrix_provider)
        # OR-Tools выполняет синхронный поиск; не блокируем цикл обработки HTTP.
        result = await asyncio.to_thread(self._solve_sync, data, travel)
        result.travel_matrices = travel.minutes
        result.travel_time_seconds_matrices = travel.seconds
        result.distance_matrices = travel.meters
        return result

    def _solve_sync(
        self, data: PlanningInput, travel: RoutingMatrices
    ) -> PlanningResult:
        solve_started = monotonic_time.perf_counter()
        model = DailyRoutingModel(data, travel)
        model.build()
        if model.objective_ranges["solve_strategy"] == "LEXICOGRAPHIC_STAGES":
            return solve_lexicographically(data, travel, model, solve_started)
        assignment, solver_time_ms = _search(model.routing, data, solve_started)
        result = extract_result(data, travel, model, assignment)
        result.solver_time_ms = solver_time_ms
        _set_solver_status(result, model.routing, data, solver_time_ms)
        return result


class OrToolsPlanningSolverFactory:
    def __init__(self, matrix_factory: TravelMatrixProviderFactory):
        self._matrix_factory = matrix_factory

    def create(self, provider: str) -> OrToolsPlanningSolver:
        matrix_provider = self._matrix_factory.create(provider)
        return OrToolsPlanningSolver(matrix_provider)
