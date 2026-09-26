"""Преобразование решения OR-Tools в маршруты, причины отказа и метрики."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ortools.constraint_solver import pywrapcp

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

from .model import DailyRoutingModel
from .objective import calculate_objective_ranges, fixed_distance_units, penalty_metrics
from .travel import RoutingMatrices


def empty_result(data: PlanningInput) -> PlanningResult:
    """Вернуть прежний EMPTY-результат без обращения к дорожному провайдеру."""
    drop_cost = sum(item.drop_penalty for item in data.pre_unassigned)
    profiles = {
        TransportType(engineer.transport_type).routing_profile
        for engineer in data.engineers
    }
    empty_matrices = {profile: [] for profile in profiles}
    objective_ranges = calculate_objective_ranges(data, empty_matrices, empty_matrices)
    return PlanningResult(
        routes=[],
        unassigned=list(data.pre_unassigned),
        solver_status="EMPTY",
        objective=max(fixed_distance_units(data).values(), default=0)
        * objective_ranges["weights"]["w_max_distance"],
        drop_cost=drop_cost,
        travel_cost=0,
        solver_time_ms=0,
        travel_matrices={},
        distance_matrices={},
        objective_metrics={
            **objective_ranges,
            **penalty_metrics(data),
            "fixed_active_engineer_count": len(data.fixed_active_engineer_ids),
            "newly_activated_engineer_count": 0,
            "used_engineer_count": len(data.fixed_active_engineer_ids),
            "empty_vehicle_count": len(data.engineers),
            "total_distance_meters": sum(
                sum(legs) for legs in data.fixed_distance_legs_by_engineer.values()
            ),
            "max_engineer_distance_meters": max(
                (sum(legs) for legs in data.fixed_distance_legs_by_engineer.values()),
                default=0,
            ),
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


def extract_result(
    data: PlanningInput,
    travel: RoutingMatrices,
    model: DailyRoutingModel,
    assignment: pywrapcp.Assignment,
) -> PlanningResult:
    """Прочитать выбранные цепочки и построить самое раннее расписание.

    OR-Tools выбирает порядок. Времена считаем последовательно: окончание
    предыдущей работы → дорога → ожидание окна → начало → окончание.
    Независимые нижние границы CumulVar не задают согласованное расписание.
    """
    objective_ranges = model.objective_ranges
    routes: list[Route] = []
    assigned_ids: set[int] = set()
    travel_cost = 0
    full_distances = {
        engineer_id: sum(legs)
        for engineer_id, legs in data.fixed_distance_legs_by_engineer.items()
    }
    for vehicle, engineer in enumerate(data.engineers):
        route = _extract_route(data, travel, model, assignment, vehicle)
        if route is None:
            continue
        routes.append(route)
        assigned_ids.update(item.job_id for item in route.jobs)
        travel_cost += route.total_travel_min * data.config.travel_cost_per_minute
        full_distances[engineer.id] = (
            full_distances.get(engineer.id, 0) + route.total_distance_meters
        )

    unassigned = list(data.pre_unassigned)
    for job in data.jobs:
        if job.id in assigned_ids:
            continue
        reason = UnassignedReasonResolver().resolve_optimizer_drop(
            job, data, travel.minutes, model.compatible_vehicles[job.id]
        )
        unassigned.append(
            UnassignedJob(
                job_id=job.id,
                drop_penalty=job.drop_penalty,
                reason_code=reason,
            )
        )
    drop_cost = sum(item.drop_penalty for item in unassigned)
    model_job_ids = {job.id for job in data.jobs}
    business_drop_cost = sum(
        item.drop_penalty // objective_ranges["drop_cost_divisor"]
        for item in unassigned
        if item.job_id in model_job_ids
    )
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
            **penalty_metrics(data),
            **(
                {
                    "objective_stage": model.objective_stage,
                    "stage_weights": model.weights,
                    "locked_drop_objectives": model.locked_drop_objectives,
                }
                if model.objective_stage != "COMBINED"
                else {}
            ),
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
            "total_distance_meters": sum(full_distances.values()),
            "max_engineer_distance_meters": max(full_distances.values(), default=0),
            "total_travel_minutes": sum(route.total_travel_min for route in routes),
            "business_drop_cost": business_drop_cost,
            "vehicle_fixed_costs": {
                str(engineer.id): (
                    0
                    if engineer.id in data.fixed_active_engineer_ids
                    else model.weights["w_engineer"]
                )
                for engineer in data.engineers
            },
        },
    )


def _extract_route(
    data: PlanningInput,
    travel: RoutingMatrices,
    model: DailyRoutingModel,
    assignment: pywrapcp.Assignment,
    vehicle: int,
) -> Route | None:
    """Пройти цепочку NextVar одного инженера от старта до фиктивного конца."""
    engineer = data.engineers[vehicle]
    routing, manager = model.routing, model.manager
    time_dimension = model.time_dimension
    index = routing.Start(vehicle)
    # Берём порядок из OR-Tools, а расписание строим последовательно.
    # Границы временных интервалов в assignment связаны между собой.
    route_start_min = engineer.shift_start_min
    previous_node = manager.IndexToNode(index)
    route_jobs: list[RouteJob] = []
    total_travel = total_service = total_waiting = 0
    profile = TransportType(engineer.transport_type).routing_profile
    matrix = travel.minutes[profile]
    distance_matrix = travel.meters[profile]
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
        model_transit = time_dimension.GetTransitValue(from_index, index, vehicle)
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
            raise RuntimeError(
                "Solver returned an invalid timetable for "
                f"job {job.id}: arrival={arrival_min}, start={start_min}, "
                f"window={job.window_start_min}-{job.window_end_min}, "
                f"finish={finish_min}, shift_end={engineer.shift_end_min}, "
                f"engineer={engineer.id}"
            )
        planned_start = _utc_at(data, start_min)
        route_jobs.append(
            RouteJob(
                job_id=job.id,
                sequence=len(route_jobs) + 1,
                planned_arrival=_utc_at(data, arrival_min),
                planned_start=planned_start,
                planned_finish=planned_start + timedelta(minutes=job.duration_min),
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
        previous_node = node
    if not route_jobs:
        return None

    last_service = _job(data.jobs, route_jobs[-1].job_id).duration_min
    end_transit = time_dimension.GetTransitValue(index, routing.End(vehicle), vehicle)
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
    return Route(
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
