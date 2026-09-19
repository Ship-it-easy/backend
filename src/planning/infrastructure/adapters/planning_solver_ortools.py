import asyncio
import math
import time as monotonic_time
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from planning.application.interfaces.travel_matrix_provider import (
    TravelMatrix,
    TravelMatrixProvider,
)
from planning.application.services.planning_input_normalizer import is_base_compatible
from planning.application.services.unassigned_reason_resolver import (
    UnassignedReasonResolver,
)
from planning.domain.entities.job import Job, UnassignedJob
from planning.domain.entities.planning import (
    PlanningInput,
    PlanningResult,
    Route,
    RouteJob,
)
from planning.domain.enums import TransportType
from planning.infrastructure.adapters.travel_matrix_provider_factory import (
    TravelMatrixProviderFactory,
)

BLOCKED_MINUTES = 100_000


class _InvalidSolverTimetable(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        vehicle: int,
        from_node: int,
        to_node: int,
    ):
        super().__init__(message)
        self.vehicle = vehicle
        self.from_node = from_node
        self.to_node = to_node


class OrToolsPlanningSolver:
    def __init__(self, matrix_provider: TravelMatrixProvider):
        self._matrix_provider = matrix_provider

    async def solve(self, data: PlanningInput) -> PlanningResult:
        if not data.jobs or not data.engineers:
            drop_cost = sum(item.drop_penalty for item in data.pre_unassigned)
            profiles = {
                "auto"
                if engineer.transport_type == TransportType.CAR
                else "pedestrian"
                for engineer in data.engineers
            }
            empty_matrices = {profile: [] for profile in profiles}
            objective_ranges = _objective_ranges(
                data, empty_matrices, empty_matrices
            )
            return PlanningResult(
                routes=[],
                unassigned=list(data.pre_unassigned),
                solver_status="EMPTY",
                objective=0,
                drop_cost=drop_cost,
                travel_cost=0,
                solver_time_ms=0,
                travel_matrices={},
                distance_matrices={},
                objective_metrics={
                    **objective_ranges,
                    **_penalty_metrics(data),
                    "fixed_active_engineer_count": len(
                        data.fixed_active_engineer_ids
                    ),
                    "newly_activated_engineer_count": 0,
                    "used_engineer_count": len(data.fixed_active_engineer_ids),
                    "empty_vehicle_count": len(data.engineers),
                    "total_distance_meters": 0,
                    "max_engineer_distance_meters": 0,
                    "total_travel_minutes": 0,
                    "vehicle_fixed_costs": {
                        str(engineer.id): (
                            0
                            if engineer.id in data.fixed_active_engineer_ids
                            else objective_ranges["weights"]["w_engineer"]
                        )
                        for engineer in data.engineers
                    },
                },
            )
        coordinates = [job.coordinate for job in data.jobs] + [
            engineer.coordinate for engineer in data.engineers
        ]
        profiles = {
            "auto" if engineer.transport_type == TransportType.CAR else "pedestrian"
            for engineer in data.engineers
        }
        try:
            raw_matrices = {
                profile: await self._matrix_provider.get_matrix(
                    coordinates,
                    profile,
                    data.config.travel_cache_ttl_days,
                )
                for profile in profiles
            }
        except Exception as error:
            raise RuntimeError("TRAVEL_PROVIDER_UNAVAILABLE") from error
        matrices: dict[str, list[list[int | None]]] = {}
        seconds_matrices: dict[str, list[list[int | None]]] = {}
        distance_matrices: dict[str, list[list[int | None]]] = {}
        for profile, raw in raw_matrices.items():
            if isinstance(raw, TravelMatrix):
                expected_size = len(coordinates)
                if (
                    len(raw.travel_time_seconds) != expected_size
                    or len(raw.distance_meters) != expected_size
                    or any(
                        len(time_row) != expected_size
                        or len(distance_row) != expected_size
                        or len(time_row) != len(distance_row)
                        for time_row, distance_row in zip(
                            raw.travel_time_seconds,
                            raw.distance_meters,
                            strict=True,
                        )
                    )
                ):
                    raise RuntimeError("DISTANCE_DATA_NOT_READY")
                matrices[profile] = [
                    [None if value is None else math.ceil(value / 60) for value in row]
                    for row in raw.travel_time_seconds
                ]
                seconds_matrices[profile] = raw.travel_time_seconds
                distance_matrices[profile] = raw.distance_meters
                for time_row, distance_row in zip(
                    raw.travel_time_seconds, raw.distance_meters, strict=True
                ):
                    if any(
                        (time_value is None) != (distance_value is None)
                        for time_value, distance_value in zip(
                            time_row, distance_row, strict=True
                        )
                    ):
                        raise RuntimeError("DISTANCE_DATA_NOT_READY")
            else:
                raise RuntimeError("DISTANCE_DATA_NOT_READY")
        self._ensure_required_arcs_have_distance(
            data,
            seconds_matrices,
            distance_matrices,
        )
        result = await asyncio.to_thread(
            self._solve_sync, data, matrices, seconds_matrices, distance_matrices
        )
        result.travel_matrices = matrices
        result.travel_time_seconds_matrices = seconds_matrices
        result.distance_matrices = distance_matrices
        return result

    @staticmethod
    def _ensure_required_arcs_have_distance(
        data: PlanningInput,
        seconds_matrices: dict[str, list[list[int | None]]],
        distance_matrices: dict[str, list[list[int | None]]],
    ) -> None:
        """Reject a partial matrix before it can bias or publish a solution."""

        job_count = len(data.jobs)
        for vehicle, engineer in enumerate(data.engineers):
            profile = (
                "auto"
                if engineer.transport_type == TransportType.CAR
                else "pedestrian"
            )
            time_matrix = seconds_matrices[profile]
            distance_matrix = distance_matrices[profile]
            compatible = [
                index
                for index, job in enumerate(data.jobs)
                if is_base_compatible(job, engineer)
                and engineer.shift_start_min <= job.window_end_min
                and engineer.shift_end_min - job.duration_min
                >= job.window_start_min
            ]
            start_node = job_count + vehicle
            required_arcs = [
                *((start_node, destination) for destination in compatible),
                *(
                    (source, destination)
                    for source in compatible
                    for destination in compatible
                    if source != destination
                ),
            ]
            if any(
                time_matrix[source][destination] is None
                or distance_matrix[source][destination] is None
                for source, destination in required_arcs
            ):
                raise RuntimeError("DISTANCE_DATA_NOT_READY")

    def _solve_sync(
        self,
        data: PlanningInput,
        matrices: dict[str, list[list[int | None]]],
        seconds_matrices: dict[str, list[list[int | None]]],
        distance_matrices: dict[str, list[list[int | None]]],
        forbidden_arcs: frozenset[tuple[int, int, int]] = frozenset(),
        solve_started: float | None = None,
    ) -> PlanningResult:
        solve_started = solve_started or monotonic_time.perf_counter()
        job_count, vehicle_count = len(data.jobs), len(data.engineers)
        end_node = job_count + vehicle_count
        starts = [job_count + vehicle for vehicle in range(vehicle_count)]
        manager = pywrapcp.RoutingIndexManager(
            end_node + 1, vehicle_count, starts, [end_node] * vehicle_count
        )
        routing = pywrapcp.RoutingModel(manager)
        transit_callbacks: list[int] = []
        cost_callbacks: list[int] = []
        distance_callbacks: list[int] = []
        objective_ranges = _objective_ranges(data, seconds_matrices, distance_matrices)
        weights = objective_ranges["weights"]

        for vehicle, engineer in enumerate(data.engineers):
            profile = (
                "auto" if engineer.transport_type == TransportType.CAR else "pedestrian"
            )
            matrix = matrices[profile]
            seconds_matrix = seconds_matrices[profile]
            distance_matrix = distance_matrices[profile]

            def transit(from_index, to_index, matrix=matrix):
                from_node = manager.IndexToNode(from_index)
                to_node = manager.IndexToNode(to_index)
                service = (
                    data.jobs[from_node].duration_min if from_node < job_count else 0
                )
                if to_node == end_node:
                    # There is no return trip, but the last job's service must
                    # finish before the vehicle's end-of-shift cumul bound.
                    return service
                travel = matrix[from_node][to_node]
                return service + (BLOCKED_MINUTES if travel is None else travel)

            def cost(
                from_index,
                to_index,
                matrix=matrix,
                seconds_matrix=seconds_matrix,
                distance_matrix=distance_matrix,
            ):
                from_node = manager.IndexToNode(from_index)
                to_node = manager.IndexToNode(to_index)
                if to_node == end_node:
                    return 0
                travel_seconds = seconds_matrix[from_node][to_node]
                distance = distance_matrix[from_node][to_node]
                if travel_seconds is None or distance is None:
                    return BLOCKED_MINUTES * weights["w_total_distance"]
                distance_units = math.ceil(distance / data.config.distance_unit_meters)
                time_units = math.ceil(
                    travel_seconds / data.config.time_unit_seconds
                )
                return distance_units * weights["w_total_distance"] + time_units

            def distance_cost(from_index, to_index, distance_matrix=distance_matrix):
                from_node = manager.IndexToNode(from_index)
                to_node = manager.IndexToNode(to_index)
                if to_node == end_node:
                    return 0
                value = distance_matrix[from_node][to_node]
                return (
                    BLOCKED_MINUTES
                    if value is None
                    else math.ceil(value / data.config.distance_unit_meters)
                )

            transit_index = routing.RegisterTransitCallback(transit)
            cost_index = routing.RegisterTransitCallback(cost)
            distance_index = routing.RegisterTransitCallback(distance_cost)
            transit_callbacks.append(transit_index)
            cost_callbacks.append(cost_index)
            distance_callbacks.append(distance_index)
            routing.SetArcCostEvaluatorOfVehicle(cost_index, vehicle)
            routing.SetFixedCostOfVehicle(
                0
                if engineer.id in data.fixed_active_engineer_ids
                else weights["w_engineer"],
                vehicle,
            )

        routing.AddDimensionWithVehicleTransits(
            transit_callbacks,
            1440,
            2880,
            False,
            "Time",
        )
        time_dimension = routing.GetDimensionOrDie("Time")
        routing.AddDimensionWithVehicleTransits(
            distance_callbacks,
            0,
            objective_ranges["dmax"],
            True,
            "Distance",
        )
        distance_dimension = routing.GetDimensionOrDie("Distance")
        distance_dimension.SetGlobalSpanCostCoefficient(weights["w_max_distance"])
        for vehicle, engineer in enumerate(data.engineers):
            earliest = engineer.shift_start_min
            # The published timetable is reconstructed from the beginning of
            # the available shift. Fix the model to the same boundary instead
            # of letting OR-Tools choose a floating route start: otherwise the
            # selected sequence and the deterministic published timetable may
            # describe different schedules.
            time_dimension.CumulVar(routing.Start(vehicle)).SetValue(earliest)
            time_dimension.CumulVar(routing.End(vehicle)).SetRange(
                earliest, engineer.shift_end_min
            )

        compatible_vehicles: dict[int, list[int]] = {}
        for job_index, job in enumerate(data.jobs):
            index = manager.NodeToIndex(job_index)
            compatible = [
                vehicle
                for vehicle, engineer in enumerate(data.engineers)
                if is_base_compatible(job, engineer)
                and engineer.shift_start_min <= job.window_end_min
                and engineer.shift_end_min - job.duration_min >= job.window_start_min
            ]
            compatible_vehicles[job.id] = compatible
            if job.mandatory and not compatible:
                raise RuntimeError("OR-Tools did not return a feasible solution")
            latest_start = (
                max(
                    data.engineers[vehicle].shift_end_min - job.duration_min
                    for vehicle in compatible
                )
                if compatible
                else job.window_end_min
            )
            time_dimension.CumulVar(index).SetRange(
                job.window_start_min,
                min(job.window_end_min, latest_start)
                if compatible
                else job.window_end_min,
            )
            routing.VehicleVar(index).SetValues(
                compatible if job.mandatory else compatible + [-1]
            )
            if not job.mandatory:
                business_drop_cost = (
                    job.drop_penalty // objective_ranges["drop_cost_divisor"]
                )
                routing.AddDisjunction([index], business_drop_cost * weights["w_drop"])
            for vehicle in compatible:
                not_assigned_to_vehicle = routing.solver().IsDifferentCstVar(
                    routing.VehicleVar(index), vehicle
                )
                routing.solver().Add(
                    time_dimension.CumulVar(index)
                    <= data.engineers[vehicle].shift_end_min
                    - job.duration_min
                    + 2880 * not_assigned_to_vehicle
                )

        self._forbid_missing_arcs(routing, manager, data, matrices, distance_matrices)
        self._forbid_time_infeasible_arcs(routing, manager, data, matrices)
        self._forbid_invalid_route_arcs(
            routing,
            manager,
            data,
            forbidden_arcs,
        )

        self._add_equipment_constraints(routing, manager, data, compatible_vehicles)
        parameters = pywrapcp.DefaultRoutingSearchParameters()
        parameters.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
        )
        # Stop at a local optimum instead of consuming the entire limit after a
        # feasible result; the remaining budget is available for the bounded
        # invalid-arc repair below when post-validation rejects a sequence.
        parameters.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GREEDY_DESCENT
        )
        # Publication validation depends on correlated route-wide time bounds,
        # so use complete Time-dimension propagation during search.
        parameters.use_full_propagation = True
        remaining_ms = int(
            (
                data.config.solver_time_limit_sec
                - (monotonic_time.perf_counter() - solve_started)
            )
            * 1000
        )
        if remaining_ms <= 0:
            raise RuntimeError("OR-Tools solver time limit exhausted")
        parameters.time_limit.FromMilliseconds(remaining_ms)
        routing.solver().ReSeed(data.config.solver_seed)
        assignment = routing.SolveWithParameters(parameters)
        solver_time_ms = int((monotonic_time.perf_counter() - solve_started) * 1000)
        if assignment is None:
            raise RuntimeError("OR-Tools did not return a feasible solution")
        try:
            result = self._extract(
                data,
                matrices,
                distance_matrices,
                manager,
                routing,
                time_dimension,
                assignment,
                compatible_vehicles,
                objective_ranges,
            )
        except _InvalidSolverTimetable as error:
            invalid_arc = (error.vehicle, error.from_node, error.to_node)
            if invalid_arc in forbidden_arcs:
                raise RuntimeError(str(error)) from error
            return self._solve_sync(
                data,
                matrices,
                seconds_matrices,
                distance_matrices,
                forbidden_arcs | {invalid_arc},
                solve_started,
            )
        result.solver_time_ms = solver_time_ms
        routing_status = routing.status()
        search_status = routing_enums_pb2.RoutingSearchStatus.Value
        if routing_status == (
            search_status.ROUTING_PARTIAL_SUCCESS_LOCAL_OPTIMUM_NOT_REACHED
        ):
            result.solver_status = "FEASIBLE_TIME_LIMIT"
        elif routing_status == search_status.ROUTING_OPTIMAL:
            result.solver_status = "OPTIMAL"
        else:
            result.solver_status = "FEASIBLE"
        return result

    def _forbid_missing_arcs(
        self,
        routing,
        manager,
        data: PlanningInput,
        matrices: dict[str, list[list[int | None]]],
        distance_matrices: dict[str, list[list[int | None]]],
    ) -> None:
        """Remove arcs that the routing provider explicitly marked unavailable."""
        solver = routing.solver()
        job_count = len(data.jobs)
        for vehicle, engineer in enumerate(data.engineers):
            profile = (
                "auto" if engineer.transport_type == TransportType.CAR else "pedestrian"
            )
            matrix = matrices[profile]
            distance_matrix = distance_matrices[profile]
            start_index = routing.Start(vehicle)
            start_node = job_count + vehicle
            for to_node in range(job_count):
                to_index = manager.NodeToIndex(to_node)
                if (
                    matrix[start_node][to_node] is None
                    or distance_matrix[start_node][to_node] is None
                ):
                    routing.NextVar(start_index).RemoveValue(to_index)
            for from_node in range(job_count):
                from_index = manager.NodeToIndex(from_node)
                for to_node in range(job_count):
                    if from_node == to_node or (
                        matrix[from_node][to_node] is not None
                        and distance_matrix[from_node][to_node] is not None
                    ):
                        continue
                    to_index = manager.NodeToIndex(to_node)
                    from_not_vehicle = solver.IsDifferentCstVar(
                        routing.VehicleVar(from_index), vehicle
                    )
                    to_not_vehicle = solver.IsDifferentCstVar(
                        routing.VehicleVar(to_index), vehicle
                    )
                    not_successor = solver.IsDifferentCstVar(
                        routing.NextVar(from_index), to_index
                    )
                    solver.Add(from_not_vehicle + to_not_vehicle + not_successor >= 1)

    def _forbid_invalid_route_arcs(
        self,
        routing,
        manager,
        data: PlanningInput,
        forbidden_arcs: frozenset[tuple[int, int, int]],
    ) -> None:
        solver = routing.solver()
        job_count = len(data.jobs)
        for vehicle, from_node, to_node in forbidden_arcs:
            to_index = manager.NodeToIndex(to_node)
            if from_node >= job_count:
                routing.NextVar(routing.Start(vehicle)).RemoveValue(to_index)
                continue
            from_index = manager.NodeToIndex(from_node)
            from_not_vehicle = solver.IsDifferentCstVar(
                routing.VehicleVar(from_index), vehicle
            )
            to_not_vehicle = solver.IsDifferentCstVar(
                routing.VehicleVar(to_index), vehicle
            )
            not_successor = solver.IsDifferentCstVar(
                routing.NextVar(from_index), to_index
            )
            solver.Add(from_not_vehicle + to_not_vehicle + not_successor >= 1)

    def _forbid_time_infeasible_arcs(
        self,
        routing,
        manager,
        data: PlanningInput,
        matrices: dict[str, list[list[int | None]]],
    ) -> None:
        """Remove arcs that cannot satisfy a shift and the destination window.

        These are necessary pairwise bounds, not a replacement solver. They keep
        an impossible sequence out of the routing search before its time limit is
        spent and the complete Time dimension still validates the whole route.
        """
        solver = routing.solver()
        job_count = len(data.jobs)
        for vehicle, engineer in enumerate(data.engineers):
            profile = (
                "auto" if engineer.transport_type == TransportType.CAR else "pedestrian"
            )
            matrix = matrices[profile]
            start_index = routing.Start(vehicle)
            start_node = job_count + vehicle
            for to_node, destination in enumerate(data.jobs):
                travel = matrix[start_node][to_node]
                if travel is None:
                    continue
                arrival = engineer.shift_start_min + travel
                start = max(arrival, destination.window_start_min)
                if (
                    start > destination.window_end_min
                    or start + destination.duration_min > engineer.shift_end_min
                ):
                    routing.NextVar(start_index).RemoveValue(
                        manager.NodeToIndex(to_node)
                    )

            for from_node, source in enumerate(data.jobs):
                from_index = manager.NodeToIndex(from_node)
                earliest_source_start = max(
                    engineer.shift_start_min, source.window_start_min
                )
                for to_node, destination in enumerate(data.jobs):
                    if from_node == to_node:
                        continue
                    travel = matrix[from_node][to_node]
                    if travel is None:
                        continue
                    arrival = earliest_source_start + source.duration_min + travel
                    start = max(arrival, destination.window_start_min)
                    if (
                        start <= destination.window_end_min
                        and start + destination.duration_min <= engineer.shift_end_min
                    ):
                        continue
                    to_index = manager.NodeToIndex(to_node)
                    from_not_vehicle = solver.IsDifferentCstVar(
                        routing.VehicleVar(from_index), vehicle
                    )
                    to_not_vehicle = solver.IsDifferentCstVar(
                        routing.VehicleVar(to_index), vehicle
                    )
                    not_successor = solver.IsDifferentCstVar(
                        routing.NextVar(from_index), to_index
                    )
                    solver.Add(from_not_vehicle + to_not_vehicle + not_successor >= 1)

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
            newly_allocated_variables = []
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
                engineer_id = data.engineers[vehicle].id
                if equipment_id not in data.preallocated_equipment_by_engineer.get(
                    engineer_id, frozenset()
                ):
                    newly_allocated_variables.append(uses)
            already_allocated = sum(
                equipment_id in values
                for values in data.preallocated_equipment_by_engineer.values()
            )
            remaining_units = max(
                0, data.equipment_units.get(equipment_id, 0) - already_allocated
            )
            if newly_allocated_variables:
                solver.Add(solver.Sum(newly_allocated_variables) <= remaining_units)

    def _extract(
        self,
        data: PlanningInput,
        matrices: dict[str, list[list[int | None]]],
        distance_matrices: dict[str, list[list[int | None]]],
        manager,
        routing,
        time_dimension,
        assignment,
        compatible_vehicles: dict[int, list[int]],
        objective_ranges: dict[str, object],
    ) -> PlanningResult:
        routes: list[Route] = []
        assigned_ids: set[int] = set()
        travel_cost = 0
        total_distance = 0
        max_distance = 0
        for vehicle, engineer in enumerate(data.engineers):
            index = routing.Start(vehicle)
            # The routing assignment may keep cumulative time variables as
            # correlated intervals. Build one deterministic earliest timetable
            # from the OR-Tools-selected sequence instead of reading interval
            # bounds independently.
            route_start_min = engineer.shift_start_min
            previous_node = manager.IndexToNode(index)
            route_jobs: list[RouteJob] = []
            total_travel = total_service = total_waiting = 0
            profile = (
                "auto" if engineer.transport_type == TransportType.CAR else "pedestrian"
            )
            matrix = matrices[profile]
            distance_matrix = distance_matrices[profile]
            route_distance = 0
            while not routing.IsEnd(assignment.Value(routing.NextVar(index))):
                from_index = index
                index = assignment.Value(routing.NextVar(from_index))
                node = manager.IndexToNode(index)
                job = data.jobs[node]
                travel = matrix[previous_node][node]
                distance = distance_matrix[previous_node][node]
                if travel is None or distance is None:
                    raise RuntimeError("Solver selected a forbidden travel arc")
                expected_transit = (
                    data.jobs[previous_node].duration_min if route_jobs else 0
                ) + travel
                model_transit = time_dimension.GetTransitValue(
                    from_index, index, vehicle
                )
                if model_transit != expected_transit:
                    raise RuntimeError(
                        "Solver transit differs from route data for "
                        f"engineer {engineer.id}: {model_transit} != "
                        f"{expected_transit} on {previous_node}->{node}"
                    )
                previous_finish = (
                    route_start_min
                    if not route_jobs
                    else _minute_of(route_jobs[-1].planned_finish, data.timezone)
                )
                arrival_min = previous_finish + travel
                start_min = max(arrival_min, job.window_start_min)
                waiting = start_min - arrival_min
                finish_min = start_min + job.duration_min
                if (
                    waiting < 0
                    or start_min < job.window_start_min
                    or start_min > job.window_end_min
                    or finish_min > engineer.shift_end_min
                ):
                    raise _InvalidSolverTimetable(
                        "Solver returned an invalid timetable for "
                        f"job {job.id}: arrival={arrival_min}, start={start_min}, "
                        f"window={job.window_start_min}-{job.window_end_min}, "
                        f"finish={finish_min}, shift_end={engineer.shift_end_min}, "
                        f"engineer={engineer.id}",
                        vehicle=vehicle,
                        from_node=previous_node,
                        to_node=node,
                    )
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
                        distance_from_previous_meters=distance,
                    )
                )
                total_travel += travel
                total_service += job.duration_min
                total_waiting += waiting
                route_distance += distance
                assigned_ids.add(job.id)
                previous_node = node
            if route_jobs:
                last_service = _job(data.jobs, route_jobs[-1].job_id).duration_min
                end_transit = time_dimension.GetTransitValue(
                    index, routing.End(vehicle), vehicle
                )
                if end_transit != last_service:
                    raise RuntimeError(
                        "Solver end transit omits the last service for "
                        f"engineer {engineer.id}: {end_transit} != {last_service}"
                    )
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
                        total_distance_meters=route_distance,
                    )
                )
                travel_cost += total_travel * data.config.travel_cost_per_minute
                total_distance += route_distance
                max_distance = max(max_distance, route_distance)

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
            objective=int(assignment.ObjectiveValue()),
            drop_cost=drop_cost,
            travel_cost=travel_cost,
            solver_time_ms=0,
            objective_metrics={
                **objective_ranges,
                **_penalty_metrics(data),
                "fixed_active_engineer_count": len(data.fixed_active_engineer_ids),
                "newly_activated_engineer_count": sum(
                    route.engineer_id not in data.fixed_active_engineer_ids
                    for route in routes
                ),
                "used_engineer_count": len(data.fixed_active_engineer_ids)
                + sum(
                    route.engineer_id not in data.fixed_active_engineer_ids
                    for route in routes
                ),
                "empty_vehicle_count": len(data.engineers) - len(routes),
                "total_distance_meters": total_distance,
                "max_engineer_distance_meters": max_distance,
                "total_travel_minutes": sum(route.total_travel_min for route in routes),
                "vehicle_fixed_costs": {
                    str(engineer.id): (
                        0
                        if engineer.id in data.fixed_active_engineer_ids
                        else objective_ranges["weights"]["w_engineer"]
                    )
                    for engineer in data.engineers
                },
            },
        )


class OrToolsPlanningSolverFactory:
    def __init__(self, matrix_factory: TravelMatrixProviderFactory):
        self._matrix_factory = matrix_factory

    def create(self, provider: str) -> OrToolsPlanningSolver:
        matrix_provider = self._matrix_factory.create(provider)
        return OrToolsPlanningSolver(matrix_provider)


def _objective_ranges(
    data: PlanningInput,
    time_seconds_matrices: dict[str, list[list[int | None]]],
    distance_matrices: dict[str, list[list[int | None]]],
) -> dict[str, object]:
    """Build the proven lexicographic integer ranges required by TZ 8.4."""

    nmax = len(data.jobs)
    vehicle_ids = {engineer.id for engineer in data.engineers}
    emax = len(data.engineers) + len(data.fixed_active_engineer_ids - vehicle_ids)
    admissible_arcs: list[tuple[int, int]] = []
    for vehicle, engineer in enumerate(data.engineers):
        profile = (
            "auto" if engineer.transport_type == TransportType.CAR else "pedestrian"
        )
        time_matrix = time_seconds_matrices[profile]
        distance_matrix = distance_matrices[profile]
        compatible = [
            index
            for index, job in enumerate(data.jobs)
            if is_base_compatible(job, engineer)
            and engineer.shift_start_min <= job.window_end_min
            and engineer.shift_end_min - job.duration_min >= job.window_start_min
        ]
        start_node = nmax + vehicle
        for destination in compatible:
            travel = time_matrix[start_node][destination]
            distance = distance_matrix[start_node][destination]
            if travel is not None and distance is not None:
                admissible_arcs.append((distance, travel))
        for source in compatible:
            for destination in compatible:
                if source == destination:
                    continue
                travel = time_matrix[source][destination]
                distance = distance_matrix[source][destination]
                if travel is not None and distance is not None:
                    admissible_arcs.append((distance, travel))
    distance_values = [
        math.ceil(distance / data.config.distance_unit_meters)
        for distance, _ in admissible_arcs
    ]
    time_values = [
        math.ceil(travel / data.config.time_unit_seconds)
        for _, travel in admissible_arcs
    ]
    admax = max(distance_values, default=0)
    atmax = max(time_values, default=0)
    route_budgets = {
        str(engineer.id): math.ceil(
            max(0, engineer.shift_end_min - engineer.shift_start_min)
            * 60
            / data.config.time_unit_seconds
        )
        for engineer in data.engineers
    }
    dmax = nmax * admax
    mmax = nmax * admax
    tmax = min(nmax * atmax, sum(route_budgets.values())) if nmax else 0
    positive_drop_costs = [
        job.drop_penalty for job in data.jobs if job.drop_penalty > 0
    ]
    drop_cost_divisor = math.gcd(*positive_drop_costs) if positive_drop_costs else 1
    pmax = sum(max(0, job.drop_penalty) // drop_cost_divisor for job in data.jobs)
    w_time = 1
    w_max_distance = tmax + 1
    w_total_distance = mmax * w_max_distance + tmax + 1
    w_engineer = dmax * w_total_distance + mmax * w_max_distance + tmax + 1
    w_drop = (
        emax * w_engineer + dmax * w_total_distance + mmax * w_max_distance + tmax + 1
    )
    maximum_objective = (
        pmax * w_drop
        + emax * w_engineer
        + dmax * w_total_distance
        + mmax * w_max_distance
        + tmax
    )
    if not (
        w_max_distance > tmax
        and w_total_distance > mmax * w_max_distance + tmax
        and w_engineer > dmax * w_total_distance + mmax * w_max_distance + tmax
        and w_drop
        > emax * w_engineer + dmax * w_total_distance + mmax * w_max_distance + tmax
    ):
        raise RuntimeError("INVALID_OBJECTIVE_WEIGHTS")
    if maximum_objective >= 2**63:
        raise RuntimeError("OBJECTIVE_RANGE_OVERFLOW")
    return {
        "algorithm_version": "objective-range-v1",
        "distance_unit_meters": data.config.distance_unit_meters,
        "time_unit_seconds": data.config.time_unit_seconds,
        "nmax": nmax,
        "emax": emax,
        "admax": admax,
        "atmax": atmax,
        "pmax": pmax,
        "drop_cost_divisor": drop_cost_divisor,
        "dmax": dmax,
        "mmax": mmax,
        "tmax": tmax,
        "route_time_budgets": route_budgets,
        "maximum_objective": maximum_objective,
        "int64_fraction": maximum_objective / (2**63 - 1),
        "weights": {
            "w_time": w_time,
            "w_max_distance": w_max_distance,
            "w_total_distance": w_total_distance,
            "w_engineer": w_engineer,
            "w_drop": w_drop,
        },
    }


def _penalty_metrics(data: PlanningInput) -> dict[str, int]:
    components = data.snapshot.get("penalty_components", {})
    return {
        "original_daily_penalty_sum": sum(
            int(value.get("daily_drop_penalty_v2", 0))
            for value in components.values()
        ),
        "emergency_bonus_sum": sum(
            int(value.get("emergency_bonus", 0)) for value in components.values()
        ),
    }


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
