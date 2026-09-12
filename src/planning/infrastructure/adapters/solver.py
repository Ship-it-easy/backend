import asyncio
import time as monotonic_time
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from planning.application.interfaces import TravelMatrixProvider
from planning.application.normalizer import current_minute_ceil, is_base_compatible
from planning.application.reason_resolver import UnassignedReasonResolver
from planning.domain.enums import TransportType
from planning.domain.models import (
    Job,
    PlanningInput,
    PlanningResult,
    Route,
    RouteJob,
    UnassignedJob,
)

BLOCKED_MINUTES = 100_000


class OrToolsPlanningSolver:
    def __init__(self, matrix_provider: TravelMatrixProvider):
        self._matrix_provider = matrix_provider

    async def solve(self, data: PlanningInput) -> PlanningResult:
        started = monotonic_time.perf_counter()
        if not data.jobs or not data.engineers:
            drop_cost = sum(item.drop_penalty for item in data.pre_unassigned)
            return PlanningResult(
                routes=[],
                unassigned=list(data.pre_unassigned),
                solver_status="EMPTY",
                objective=drop_cost,
                drop_cost=drop_cost,
                travel_cost=0,
                solver_time_ms=0,
                travel_matrices={},
            )
        coordinates = [job.coordinate for job in data.jobs] + [
            engineer.coordinate for engineer in data.engineers
        ]
        profiles = {
            "auto" if engineer.transport_type == TransportType.CAR else "pedestrian"
            for engineer in data.engineers
        }
        matrices = {
            profile: await self._matrix_provider.get_matrix(coordinates, profile)
            for profile in profiles
        }
        result = await asyncio.to_thread(self._solve_sync, data, matrices)
        result.travel_matrices = matrices
        result.solver_time_ms = int((monotonic_time.perf_counter() - started) * 1000)
        if result.solver_time_ms >= data.config.solver_time_limit_sec * 950:
            result.solver_status = "FEASIBLE_TIME_LIMIT"
        return result

    def _solve_sync(
        self,
        data: PlanningInput,
        matrices: dict[str, list[list[int | None]]],
    ) -> PlanningResult:
        job_count, vehicle_count = len(data.jobs), len(data.engineers)
        end_node = job_count + vehicle_count
        starts = [job_count + vehicle for vehicle in range(vehicle_count)]
        manager = pywrapcp.RoutingIndexManager(
            end_node + 1, vehicle_count, starts, [end_node] * vehicle_count
        )
        routing = pywrapcp.RoutingModel(manager)
        transit_callbacks: list[int] = []
        cost_callbacks: list[int] = []

        for vehicle, engineer in enumerate(data.engineers):
            profile = (
                "auto" if engineer.transport_type == TransportType.CAR else "pedestrian"
            )
            matrix = matrices[profile]

            def transit(from_index, to_index, matrix=matrix):
                from_node = manager.IndexToNode(from_index)
                to_node = manager.IndexToNode(to_index)
                service = (
                    data.jobs[from_node].duration_min if from_node < job_count else 0
                )
                if to_node == end_node:
                    return service
                travel = matrix[from_node][to_node]
                return service + (BLOCKED_MINUTES if travel is None else travel)

            def cost(from_index, to_index, matrix=matrix):
                from_node = manager.IndexToNode(from_index)
                to_node = manager.IndexToNode(to_index)
                if to_node == end_node:
                    return 0
                travel = matrix[from_node][to_node]
                minutes = BLOCKED_MINUTES if travel is None else travel
                return minutes

            transit_index = routing.RegisterTransitCallback(transit)
            cost_index = routing.RegisterTransitCallback(cost)
            transit_callbacks.append(transit_index)
            cost_callbacks.append(cost_index)
            routing.SetArcCostEvaluatorOfVehicle(cost_index, vehicle)

        routing.AddDimensionWithVehicleTransits(
            transit_callbacks,
            1440,
            2880,
            False,
            "Time",
        )
        time_dimension = routing.GetDimensionOrDie("Time")
        current_minute = current_minute_ceil(data.timezone)
        for vehicle, engineer in enumerate(data.engineers):
            earliest = max(engineer.shift_start_min, current_minute)
            time_dimension.CumulVar(routing.Start(vehicle)).SetRange(
                earliest, engineer.shift_end_min
            )
            time_dimension.CumulVar(routing.End(vehicle)).SetRange(
                earliest, engineer.shift_end_min
            )

        compatible_vehicles: dict[int, list[int]] = {}
        for job_index, job in enumerate(data.jobs):
            index = manager.NodeToIndex(job_index)
            time_dimension.CumulVar(index).SetRange(
                job.window_start_min, job.window_end_min
            )
            compatible = [
                vehicle
                for vehicle, engineer in enumerate(data.engineers)
                if is_base_compatible(job, engineer)
            ]
            compatible_vehicles[job.id] = compatible
            routing.VehicleVar(index).SetValues(compatible + [-1])
            routing.AddDisjunction([index], job.drop_penalty)

        self._add_equipment_constraints(routing, manager, data, compatible_vehicles)
        parameters = pywrapcp.DefaultRoutingSearchParameters()
        parameters.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
        )
        parameters.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        parameters.time_limit.FromSeconds(data.config.solver_time_limit_sec)
        assignment = routing.SolveWithParameters(parameters)
        if assignment is None:
            raise RuntimeError("OR-Tools did not return a feasible solution")
        return self._extract(
            data,
            matrices,
            manager,
            routing,
            time_dimension,
            assignment,
            compatible_vehicles,
        )

    def _add_equipment_constraints(
        self,
        routing,
        manager,
        data: PlanningInput,
        compatible_vehicles: dict[int, list[int]],
    ) -> None:
        solver = routing.solver()
        equipment_ids = {
            equipment_id for job in data.jobs for equipment_id in job.required_equipment
        }
        for equipment_id in equipment_ids:
            uses_variables = []
            for vehicle in range(len(data.engineers)):
                uses = solver.BoolVar(f"uses_equipment_{vehicle}_{equipment_id}")
                assigned_variables = []
                for job_index, job in enumerate(data.jobs):
                    if (
                        equipment_id not in job.required_equipment
                        or vehicle not in compatible_vehicles[job.id]
                    ):
                        continue
                    assigned = solver.IsEqualCstVar(
                        routing.VehicleVar(manager.NodeToIndex(job_index)), vehicle
                    )
                    assigned_variables.append(assigned)
                    solver.Add(assigned <= uses)
                if assigned_variables:
                    solver.Add(uses <= solver.Sum(assigned_variables))
                else:
                    solver.Add(uses == 0)
                uses_variables.append(uses)
            solver.Add(
                solver.Sum(uses_variables) <= data.equipment_units.get(equipment_id, 0)
            )

    def _extract(
        self,
        data: PlanningInput,
        matrices: dict[str, list[list[int | None]]],
        manager,
        routing,
        time_dimension,
        assignment,
        compatible_vehicles: dict[int, list[int]],
    ) -> PlanningResult:
        routes: list[Route] = []
        assigned_ids: set[int] = set()
        travel_cost = 0
        for vehicle, engineer in enumerate(data.engineers):
            index = routing.Start(vehicle)
            route_start_min = assignment.Value(time_dimension.CumulVar(index))
            previous_node = manager.IndexToNode(index)
            route_jobs: list[RouteJob] = []
            total_travel = total_service = total_waiting = 0
            profile = (
                "auto" if engineer.transport_type == TransportType.CAR else "pedestrian"
            )
            matrix = matrices[profile]
            while not routing.IsEnd(assignment.Value(routing.NextVar(index))):
                index = assignment.Value(routing.NextVar(index))
                node = manager.IndexToNode(index)
                job = data.jobs[node]
                travel = matrix[previous_node][node]
                if travel is None:
                    raise RuntimeError("Solver selected a forbidden travel arc")
                previous_finish = (
                    route_start_min
                    if not route_jobs
                    else _minute_of(route_jobs[-1].planned_finish, data.timezone)
                )
                arrival_min = previous_finish + travel
                start_min = max(arrival_min, job.window_start_min)
                waiting = start_min - arrival_min
                planned_start = _utc_at(data, start_min)
                route_jobs.append(
                    RouteJob(
                        job_id=job.id,
                        sequence=len(route_jobs) + 1,
                        planned_arrival=_utc_at(data, arrival_min),
                        planned_start=planned_start,
                        planned_finish=planned_start
                        + timedelta(minutes=job.duration_min),
                        travel_from_previous_min=travel,
                        waiting_before_job_min=waiting,
                        drop_penalty=job.drop_penalty,
                    )
                )
                total_travel += travel
                total_service += job.duration_min
                total_waiting += waiting
                assigned_ids.add(job.id)
                previous_node = node
            if route_jobs:
                equipment = {
                    equipment_id
                    for item in route_jobs
                    for equipment_id in _job(data.jobs, item.job_id).required_equipment
                }
                routes.append(
                    Route(
                        engineer_id=engineer.id,
                        planned_start=_utc_at(data, route_start_min),
                        planned_finish=route_jobs[-1].planned_finish,
                        total_travel_min=total_travel,
                        total_service_min=total_service,
                        total_waiting_min=total_waiting,
                        jobs=route_jobs,
                        equipment_type_ids=equipment,
                    )
                )
                travel_cost += total_travel

        unassigned = list(data.pre_unassigned)
        for job in data.jobs:
            if job.id in assigned_ids:
                continue
            reason = UnassignedReasonResolver().resolve_optimizer_drop(
                job, data, matrices, compatible_vehicles[job.id]
            )
            unassigned.append(
                UnassignedJob(
                    job_id=job.id,
                    drop_penalty=job.drop_penalty,
                    reason_code=reason,
                )
            )
        drop_cost = sum(item.drop_penalty for item in unassigned)
        return PlanningResult(
            routes=routes,
            unassigned=unassigned,
            solver_status="FEASIBLE",
            objective=drop_cost + travel_cost,
            drop_cost=drop_cost,
            travel_cost=travel_cost,
            solver_time_ms=0,
        )


def _utc_at(data: PlanningInput, minute: int) -> datetime:
    local_midnight = datetime.combine(
        data.planning_date, time.min, tzinfo=ZoneInfo(data.timezone)
    )
    return (local_midnight + timedelta(minutes=minute)).astimezone(timezone.utc)


def _minute_of(value: datetime, timezone_name: str) -> int:
    local = value.astimezone(ZoneInfo(timezone_name))
    return local.hour * 60 + local.minute


def _job(jobs: list[Job], job_id: int) -> Job:
    return next(job for job in jobs if job.id == job_id)
