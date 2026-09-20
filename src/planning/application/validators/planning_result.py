import math
from collections import Counter
from datetime import timedelta
from zoneinfo import ZoneInfo

from planning.application.services.planning_input_normalizer import is_base_compatible
from planning.domain.entities.planning import PlanningInput, PlanningResult
from planning.domain.enums import TransportType


class PlanningValidator:
    def validate(self, data: PlanningInput, result: PlanningResult) -> list[str]:
        errors: list[str] = []
        jobs = {job.id: job for job in data.jobs}
        engineers = {engineer.id: engineer for engineer in data.engineers}
        assigned = [item.job_id for route in result.routes for item in route.jobs]
        unassigned = [item.job_id for item in result.unassigned]
        expected_job_ids = {job.id for job in data.jobs} | {
            item.job_id for item in data.pre_unassigned
        }
        duplicates = [
            job_id for job_id, count in Counter(assigned).items() if count > 1
        ]
        if duplicates:
            errors.append(f"jobs assigned more than once: {duplicates}")
        overlap = set(assigned) & set(unassigned)
        if overlap:
            errors.append(f"jobs both assigned and unassigned: {sorted(overlap)}")
        duplicate_unassigned = [
            job_id for job_id, count in Counter(unassigned).items() if count > 1
        ]
        if duplicate_unassigned:
            errors.append(f"jobs unassigned more than once: {duplicate_unassigned}")
        actual_job_ids = set(assigned) | set(unassigned)
        if actual_job_ids != expected_job_ids:
            errors.append("assigned and unassigned jobs do not exactly cover the input")
        if any(job.mandatory and job.id not in assigned for job in data.jobs):
            errors.append("mandatory job is not assigned")

        equipment_usage: Counter[int] = Counter()
        for values in data.preallocated_equipment_by_engineer.values():
            equipment_usage.update(values)
        job_indexes = {job.id: index for index, job in enumerate(data.jobs)}
        engineer_indexes = {
            engineer.id: index for index, engineer in enumerate(data.engineers)
        }
        actual_travel_time_units = 0
        for route in result.routes:
            engineer = engineers.get(route.engineer_id)
            if engineer is None:
                errors.append(f"unknown engineer {route.engineer_id}")
                continue
            expected_sequence = 1
            used_equipment: set[int] = set()
            previous_finish = route.planned_start
            previous_node = len(data.jobs) + engineer_indexes[engineer.id]
            calculated_travel = calculated_service = calculated_waiting = 0
            calculated_distance = 0
            profile = (
                "auto" if engineer.transport_type == TransportType.CAR else "pedestrian"
            )
            matrix = result.travel_matrices.get(profile)
            seconds_matrix = result.travel_time_seconds_matrices.get(profile)
            distance_matrix = result.distance_matrices.get(profile)
            for item in route.jobs:
                job = jobs.get(item.job_id)
                if job is None:
                    errors.append(f"unknown or ineligible job {item.job_id}")
                    continue
                if item.sequence != expected_sequence:
                    errors.append(f"invalid sequence for job {item.job_id}")
                expected_sequence += 1
                if not is_base_compatible(job, engineer):
                    errors.append(f"job {item.job_id} is incompatible with engineer")
                local_start = item.planned_start.astimezone(ZoneInfo(data.timezone))
                local_arrival = item.planned_arrival.astimezone(ZoneInfo(data.timezone))
                start_min = local_start.hour * 60 + local_start.minute
                local_finish = item.planned_finish.astimezone(ZoneInfo(data.timezone))
                if any(
                    value.date() != data.planning_date
                    for value in (local_arrival, local_start, local_finish)
                ):
                    errors.append(f"job {item.job_id} is planned on another date")
                if not job.window_start_min <= start_min <= job.window_end_min:
                    errors.append(f"job {item.job_id} starts outside its window")
                finish_min = local_finish.hour * 60 + local_finish.minute
                if (
                    start_min < engineer.shift_start_min
                    or finish_min > engineer.shift_end_min
                ):
                    errors.append(f"job {item.job_id} is outside engineer shift")
                expected_arrival = previous_finish + timedelta(
                    minutes=item.travel_from_previous_min
                )
                if expected_arrival != item.planned_arrival:
                    errors.append(f"job {item.job_id} has broken travel sequence")
                if item.planned_start < item.planned_arrival:
                    errors.append(f"job {item.job_id} starts before arrival")
                if (
                    int(
                        (item.planned_start - item.planned_arrival).total_seconds() / 60
                    )
                    != item.waiting_before_job_min
                ):
                    errors.append(f"job {item.job_id} has invalid waiting time")
                if (
                    int((item.planned_finish - item.planned_start).total_seconds() / 60)
                    != job.duration_min
                ):
                    errors.append(f"job {item.job_id} has invalid service duration")
                previous_finish = item.planned_finish
                job_index = job_indexes[item.job_id]
                if (
                    matrix is not None
                    and matrix[previous_node][job_index]
                    != item.travel_from_previous_min
                ):
                    errors.append(f"job {item.job_id} has invalid travel time")
                if (
                    distance_matrix is not None
                    and distance_matrix[previous_node][job_index]
                    != item.distance_from_previous_meters
                ):
                    errors.append(f"job {item.job_id} has invalid distance")
                travel_seconds = (
                    seconds_matrix[previous_node][job_index]
                    if seconds_matrix is not None
                    else item.travel_from_previous_min * 60
                )
                if travel_seconds is None:
                    errors.append(f"job {item.job_id} has no raw travel time")
                else:
                    actual_travel_time_units += math.ceil(
                        travel_seconds / data.config.time_unit_seconds
                    )
                previous_node = job_index
                calculated_travel += item.travel_from_previous_min
                calculated_service += job.duration_min
                calculated_waiting += item.waiting_before_job_min
                calculated_distance += item.distance_from_previous_meters
                used_equipment.update(job.required_equipment)
            if route.jobs and route.planned_finish != route.jobs[-1].planned_finish:
                errors.append(f"route {route.engineer_id} has invalid finish")
            if route.total_travel_min != calculated_travel:
                errors.append(f"route {route.engineer_id} has invalid travel total")
            if route.total_service_min != calculated_service:
                errors.append(f"route {route.engineer_id} has invalid service total")
            if route.total_waiting_min != calculated_waiting:
                errors.append(f"route {route.engineer_id} has invalid waiting total")
            if used_equipment != route.equipment_type_ids:
                errors.append(
                    f"route {route.engineer_id} has invalid equipment assignment"
                )
            if route.total_distance_meters != calculated_distance:
                errors.append(f"route {route.engineer_id} has invalid distance total")
            preallocated = data.preallocated_equipment_by_engineer.get(
                route.engineer_id, frozenset()
            )
            equipment_usage.update(used_equipment - preallocated)
        for equipment_id, used in equipment_usage.items():
            if used > data.equipment_units.get(equipment_id, 0):
                errors.append(f"equipment {equipment_id} capacity exceeded")
        expected_drop_cost = sum(item.drop_penalty for item in result.unassigned)
        expected_travel_cost = (
            sum(route.total_travel_min for route in result.routes)
            * data.config.travel_cost_per_minute
        )
        if result.drop_cost != expected_drop_cost:
            errors.append("invalid drop cost")
        if result.travel_cost != expected_travel_cost:
            errors.append("invalid travel cost")
        metrics = result.objective_metrics
        if metrics:
            weights = metrics["weights"]
            model_job_ids = {job.id for job in data.jobs}
            model_drop = sum(
                item.drop_penalty // metrics["drop_cost_divisor"]
                for item in result.unassigned
                if item.job_id in model_job_ids
            )
            distance_units_by_route = [
                sum(
                    math.ceil(
                        item.distance_from_previous_meters
                        / data.config.distance_unit_meters
                    )
                    for item in route.jobs
                )
                for route in result.routes
            ]
            time_units = actual_travel_time_units
            actual_total_distance = sum(distance_units_by_route)
            actual_max_distance = max(distance_units_by_route, default=0)
            actual_fixed = len(data.fixed_active_engineer_ids)
            actual_new = sum(
                route.engineer_id not in data.fixed_active_engineer_ids
                for route in result.routes
            )
            actual_used = actual_fixed + actual_new
            if model_drop > metrics["pmax"]:
                errors.append("actual drop cost exceeds Pmax")
            if actual_used > metrics["emax"]:
                errors.append("actual used engineers exceed Emax")
            if actual_total_distance > metrics["dmax"]:
                errors.append("actual total distance exceeds Dmax")
            if actual_max_distance > metrics["mmax"]:
                errors.append("actual maximum distance exceeds Mmax")
            if time_units > metrics["tmax"]:
                errors.append("actual travel time exceeds Tmax")
            if metrics["fixed_active_engineer_count"] != actual_fixed:
                errors.append("invalid fixed active engineer count")
            if metrics["newly_activated_engineer_count"] != actual_new:
                errors.append("invalid newly activated engineer count")
            if metrics["used_engineer_count"] != actual_used:
                errors.append("invalid used engineer count")
            expected_fixed_costs = {
                str(engineer.id): (
                    0
                    if engineer.id in data.fixed_active_engineer_ids
                    else weights["w_engineer"]
                )
                for engineer in data.engineers
            }
            if metrics["vehicle_fixed_costs"] != expected_fixed_costs:
                errors.append("invalid vehicle fixed costs")
            lower_objective = (
                metrics["newly_activated_engineer_count"] * weights["w_engineer"]
                + actual_total_distance * weights["w_total_distance"]
                + actual_max_distance * weights["w_max_distance"]
                + time_units
            )
            if metrics.get("solve_strategy") == "PHASED_DROP_THEN_ROUTE":
                if metrics.get("business_drop_cost") != model_drop:
                    errors.append("invalid phased business drop cost")
                expected_objective = lower_objective
            else:
                expected_objective = (
                    model_drop * weights["w_drop"] + lower_objective
                )
            if result.objective != expected_objective:
                errors.append(
                    "invalid lexicographic objective: "
                    f"actual={result.objective}, expected={expected_objective}, "
                    f"strategy={metrics.get('solve_strategy')}"
                )
            if result.objective > metrics["maximum_objective"]:
                errors.append("actual objective exceeds proven maximum")
        elif result.objective != expected_drop_cost + expected_travel_cost:
            errors.append("invalid objective")
        return errors
