"""Дневной расчёт маршрутов: дороги → модель → поиск → расписание.

Каскад дней, SLA-приоритеты, защита текущей работы и публикация находятся
в application. Здесь сохраняется публичный адаптер PlanningSolver.
Подробное объяснение: docs/ROUTING_ALGORITHM.md.
"""

import asyncio
import time as monotonic_time

from planning.application.errors import SolverNoFeasibleSolution, SolverTimeLimit
from planning.application.interfaces.travel_matrix_provider import TravelMatrixProvider
from planning.application.services.baseline_fifo import (
    baseline_applicable,
    not_applicable_result,
    pending_result,
)
from planning.domain.entities.planning import PlanningInput, PlanningResult
from planning.domain.traffic import INTERVAL_MINUTES, QUALITY, VERSION
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
    refine_district_times_for_routes,
)
from planning.infrastructure.adapters.travel_matrix_provider_factory import (
    TravelMatrixProviderFactory,
)

__all__ = ["OrToolsPlanningSolver", "OrToolsPlanningSolverFactory", "_objective_ranges"]


class OrToolsPlanningSolver:
    def __init__(
        self,
        matrix_provider: TravelMatrixProvider,
        baseline_comparison_enabled: bool = True,
    ):
        self._matrix_provider = matrix_provider
        self._baseline_comparison_enabled = baseline_comparison_enabled

    async def solve(self, data: PlanningInput) -> PlanningResult:
        """Рассчитать один день; вход уже подготовлен прикладными сервисами."""
        if not data.jobs or not data.engineers:
            result = empty_result(data)
            return await self._maybe_attach_baseline(data, result)

        travel = await prepare_travel_matrices(data, self._matrix_provider)
        # OR-Tools выполняет синхронный поиск; не блокируем цикл обработки HTTP.
        result = await asyncio.to_thread(self._solve_sync, data, travel)
        traffic_enabled = getattr(
            self._matrix_provider, "planning_traffic_enabled", False
        )
        correction_passes = 0
        correction_status = "not_needed"
        if traffic_enabled and result.routes:
            # Static matrices are required by OR-Tools.  Correct the selected
            # arcs at their real departures.  A second pass handles the case
            # where the first correction changes the chosen order.
            correction_status = "converged"
            for _ in range(4):
                before = [
                    (route.engineer_id, tuple(job.job_id for job in route.jobs))
                    for route in result.routes
                ]
                previous_result, previous_travel = result, travel
                refined_travel = refine_district_times_for_routes(data, travel, result)
                if refined_travel == travel:
                    break
                try:
                    result = await asyncio.to_thread(
                        self._solve_sync, data, refined_travel
                    )
                except SolverTimeLimit:
                    result, travel = previous_result, previous_travel
                    correction_status = "fallback_time_limit"
                    break
                except SolverNoFeasibleSolution:
                    result, travel = previous_result, previous_travel
                    correction_status = "fallback_infeasible"
                    break
                travel = refined_travel
                correction_passes += 1
                after = [
                    (route.engineer_id, tuple(job.job_id for job in route.jobs))
                    for route in result.routes
                ]
                if after == before:
                    break
            else:
                correction_status = "max_passes_reached"
        result.travel_matrices = travel.minutes
        result.travel_time_seconds_matrices = travel.seconds
        result.distance_matrices = travel.meters
        result.objective_metrics["traffic_model"] = {
            "enabled": traffic_enabled,
            "version": VERSION,
            "quality": QUALITY if traffic_enabled else "disabled",
            "mode": (
                "district_time_refined_estimate" if traffic_enabled else "baseline"
            ),
            "coverage": "all_automobile_endpoint_pairs",
            "interval_minutes": INTERVAL_MINUTES,
            "time_aware_correction_passes": correction_passes,
            "time_aware_correction_status": correction_status,
        }
        return await self._maybe_attach_baseline(data, result)

    async def _maybe_attach_baseline(
        self, data: PlanningInput, result: PlanningResult
    ) -> PlanningResult:
        if not self._baseline_comparison_enabled:
            return result
        return await self._attach_baseline(data, result)

    async def _attach_baseline(
        self, data: PlanningInput, result: PlanningResult
    ) -> PlanningResult:
        applicable, _ = baseline_applicable(data)
        if not applicable:
            result.baseline_result = not_applicable_result(data)
            return result
        # Persist a durable queue item together with the validated optimal run.
        # The executor calculates it out-of-band, so road-provider latency or a
        # baseline failure cannot delay publication of the working plan.
        result.baseline_result = pending_result(data)
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
    def __init__(
        self,
        matrix_factory: TravelMatrixProviderFactory,
        baseline_comparison_enabled: bool = True,
    ):
        self._matrix_factory = matrix_factory
        self._baseline_comparison_enabled = baseline_comparison_enabled

    def create(self, provider: str) -> OrToolsPlanningSolver:
        matrix_provider = self._matrix_factory.create(provider)
        return OrToolsPlanningSolver(
            matrix_provider,
            self._baseline_comparison_enabled,
        )
