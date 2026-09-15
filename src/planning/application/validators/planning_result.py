from collections import Counter
from datetime import timedelta
from zoneinfo import ZoneInfo

from planning.application.services.planning_input_normalizer import is_base_compatible
from planning.domain.entities.planning import PlanningInput, PlanningResult
from planning.domain.enums import TransportType


class PlanningValidator:
    def validate(
        self,
        data: PlanningInput,
        result: PlanningResult,
        project_id: int,
    ) -> list[str]:
        errors: list[str] = []
        expected_project_id = project_id
        if data.project_id != expected_project_id:
            errors.append("planning input belongs to another project")
        if data.snapshot.get("project_id") != expected_project_id:
            errors.append("planning snapshot belongs to another project")
        for job in data.jobs:
            if job.project_id != expected_project_id:
                errors.append(f"job {job.id} belongs to another project")
        for engineer in data.engineers:
            if engineer.project_id != expected_project_id:
                errors.append(f"engineer {engineer.id} belongs to another project")
        jobs = {job.id: job for job in data.jobs}
        engineers = {engineer.id: engineer for engineer in data.engineers}
        assigned = [item.job_id for route in result.routes for item in route.jobs]
        unassigned = [item.job_id for item in result.unassigned]
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
        if len(set(assigned) | set(unassigned)) != data.input_jobs_count:
            errors.append("assigned and unassigned jobs do not cover the input")

        equipment_usage: Counter[int] = Counter()
        job_indexes = {job.id: index for index, job in enumerate(data.jobs)}
        engineer_indexes = {
            engineer.id: index for index, engineer in enumerate(data.engineers)
        }
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
            profile = (
                "auto" if engineer.transport_type == TransportType.CAR else "pedestrian"
            )
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
                start_min = local_start.hour * 60 + local_start.minute
                if not job.window_start_min <= start_min <= job.window_end_min:
                    errors.append(f"job {item.job_id} starts outside its window")
                local_finish = item.planned_finish.astimezone(ZoneInfo(data.timezone))
                finish_min = local_finish.hour * 60 + local_finish.minute
                if (
                    start_min < engineer.shift_start_min
                    or finish_min > job.window_end_min
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
                matrix = result.travel_matrices.get(profile)
                if (
                    matrix is not None
                    and matrix[previous_node][job_index]
                    != item.travel_from_previous_min
                ):
                    errors.append(f"job {item.job_id} has invalid travel time")
                previous_node = job_index
                calculated_travel += item.travel_from_previous_min
                calculated_service += job.duration_min
                calculated_waiting += item.waiting_before_job_min
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
            equipment_usage.update(used_equipment)
        for equipment_id, used in equipment_usage.items():
            if equipment_id not in data.equipment_units:
                errors.append(f"equipment {equipment_id} belongs to another project")
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
        if result.objective != expected_drop_cost + expected_travel_cost:
            errors.append("invalid objective")
        return errors
