"""Проверки времени, дорог, сумм маршрута и общего оборудования дня."""

import math
from collections import Counter
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from planning.application.services.planning_input_normalizer import is_base_compatible
from planning.domain.entities.engineer import Engineer
from planning.domain.entities.job import Job
from planning.domain.entities.planning import (
    PlanningInput,
    PlanningResult,
    Route,
    RouteJob,
)
from planning.domain.enums import TransportType


class RouteChecks:
    """Контекст одного прохода по маршрутам; не запускает solver и не меняет план."""

    def __init__(
        self,
        data: PlanningInput,
        result: PlanningResult,
        jobs: dict[int, Job],
        engineers: dict[int, Engineer],
        errors: list[str],
    ):
        self.data = data
        self.result = result
        self.jobs = jobs
        self.engineers = engineers
        self.errors = errors
        self.midnight = datetime.combine(
            self.data.planning_date, time.min, ZoneInfo(self.data.timezone)
        )

        self.job_indexes = {job.id: index for index, job in enumerate(self.data.jobs)}
        self.engineer_indexes = {
            engineer.id: index for index, engineer in enumerate(self.data.engineers)
        }

    def validate(self) -> int:
        """Проверить маршруты и склад; вернуть время дороги в единицах objective."""
        equipment_usage: Counter[int] = Counter()
        for values in self.data.preallocated_equipment_by_engineer.values():
            equipment_usage.update(values)
        actual_travel_time_units = 0
        for route in self.result.routes:
            engineer = self.engineers.get(route.engineer_id)
            if engineer is None:
                self.errors.append(f"unknown engineer {route.engineer_id}")
                continue
            used_equipment, time_units = self._check_route(route, engineer)
            actual_travel_time_units += time_units
            preallocated = self.data.preallocated_equipment_by_engineer.get(
                route.engineer_id, frozenset()
            )
            # Выданное ранее уже учтено; несколько работ инженера используют
            # одну единицу типа оборудования. Поэтому здесь множество, не список.
            equipment_usage.update(used_equipment - preallocated)
        for equipment_id, used in equipment_usage.items():
            if used > self.data.equipment_units.get(equipment_id, 0):
                self.errors.append(f"equipment {equipment_id} capacity exceeded")
        return actual_travel_time_units

    def _check_route(self, route: Route, engineer: Engineer) -> tuple[set[int], int]:
        """Пройти остановки по порядку, затем проверить итоги маршрута."""
        if not route.jobs:
            self.errors.append(f"route {route.engineer_id} is empty")
        shift_start = self.midnight + timedelta(minutes=engineer.shift_start_min)
        shift_end = self.midnight + timedelta(minutes=engineer.shift_end_min)
        if not shift_start <= route.planned_start <= route.planned_finish <= shift_end:
            self.errors.append(f"route {route.engineer_id} is outside engineer shift")
        expected_sequence = 1
        used_equipment: set[int] = set()
        previous_finish = route.planned_start
        previous_node = len(self.data.jobs) + self.engineer_indexes[engineer.id]
        calculated_travel = calculated_service = calculated_waiting = 0
        calculated_distance = 0
        actual_travel_time_units = 0
        profile = TransportType(engineer.transport_type).routing_profile
        for item in route.jobs:
            job = self.jobs.get(item.job_id)
            if job is None:
                self.errors.append(f"unknown or ineligible job {item.job_id}")
                continue
            if item.sequence != expected_sequence:
                self.errors.append(f"invalid sequence for job {item.job_id}")
            expected_sequence += 1
            self._check_visit_times(item, job, engineer, previous_finish)
            previous_finish = item.planned_finish
            job_index = self.job_indexes[item.job_id]
            actual_travel_time_units += self._check_visit_travel(
                item, profile, previous_node, job_index
            )
            previous_node = job_index
            calculated_travel += item.travel_from_previous_min
            calculated_service += job.duration_min
            calculated_waiting += item.waiting_before_job_min
            calculated_distance += item.distance_from_previous_meters
            used_equipment.update(job.required_equipment)
        if route.jobs and route.planned_finish != route.jobs[-1].planned_finish:
            self.errors.append(f"route {route.engineer_id} has invalid finish")
        if route.total_travel_min != calculated_travel:
            self.errors.append(f"route {route.engineer_id} has invalid travel total")
        if route.total_service_min != calculated_service:
            self.errors.append(f"route {route.engineer_id} has invalid service total")
        if route.total_waiting_min != calculated_waiting:
            self.errors.append(f"route {route.engineer_id} has invalid waiting total")
        if used_equipment != route.equipment_type_ids:
            self.errors.append(
                f"route {route.engineer_id} has invalid equipment assignment"
            )
        if route.total_distance_meters != calculated_distance:
            self.errors.append(f"route {route.engineer_id} has invalid distance total")
        return used_equipment, actual_travel_time_units

    def _check_visit_times(
        self, item: RouteJob, job: Job, engineer: Engineer, previous_finish: datetime
    ) -> None:
        """Проверить исполнителя, дату, окно, смену и непрерывность расписания.

        Окно ограничивает начало; смена — завершение всей работы. Прибытие
        считаем от предыдущего завершения, ожидание проверяем отдельно.
        """
        if not is_base_compatible(job, engineer):
            self.errors.append(f"job {item.job_id} is incompatible with engineer")
        local_start = item.planned_start.astimezone(ZoneInfo(self.data.timezone))
        local_arrival = item.planned_arrival.astimezone(ZoneInfo(self.data.timezone))
        start_min = local_start.hour * 60 + local_start.minute
        local_finish = item.planned_finish.astimezone(ZoneInfo(self.data.timezone))
        if any(
            value.date() != self.data.planning_date
            for value in (local_arrival, local_start, local_finish)
        ):
            self.errors.append(f"job {item.job_id} is planned on another date")
        if not (
            self.midnight + timedelta(minutes=job.window_start_min)
            <= item.planned_start
            <= self.midnight + timedelta(minutes=job.window_end_min)
        ):
            self.errors.append(f"job {item.job_id} starts outside its window")
        finish_min = local_finish.hour * 60 + local_finish.minute
        if start_min < engineer.shift_start_min or finish_min > engineer.shift_end_min:
            self.errors.append(f"job {item.job_id} is outside engineer shift")
        expected_arrival = previous_finish + timedelta(
            minutes=item.travel_from_previous_min
        )
        if expected_arrival != item.planned_arrival:
            self.errors.append(f"job {item.job_id} has broken travel sequence")
        if item.planned_start < item.planned_arrival:
            self.errors.append(f"job {item.job_id} starts before arrival")
        if item.planned_start - item.planned_arrival != timedelta(
            minutes=item.waiting_before_job_min
        ):
            self.errors.append(f"job {item.job_id} has invalid waiting time")
        if item.planned_finish - item.planned_start != timedelta(
            minutes=job.duration_min
        ):
            self.errors.append(f"job {item.job_id} has invalid service duration")
        if (
            min(
                item.travel_from_previous_min,
                item.waiting_before_job_min,
                item.distance_from_previous_meters,
            )
            < 0
        ):
            self.errors.append(f"job {item.job_id} has negative route metrics")

    def _check_visit_travel(
        self, item: RouteJob, profile: str, previous_node: int, job_index: int
    ) -> int:
        """Сверить фактический переезд с матрицами и посчитать единицы времени."""
        matrix = self.result.travel_matrices.get(profile)
        seconds_matrix = self.result.travel_time_seconds_matrices.get(profile)
        distance_matrix = self.result.distance_matrices.get(profile)
        if (
            matrix is not None
            and matrix[previous_node][job_index] != item.travel_from_previous_min
        ):
            self.errors.append(f"job {item.job_id} has invalid travel time")
        if (
            distance_matrix is not None
            and distance_matrix[previous_node][job_index]
            != item.distance_from_previous_meters
        ):
            self.errors.append(f"job {item.job_id} has invalid distance")
        travel_seconds = (
            seconds_matrix[previous_node][job_index]
            if seconds_matrix is not None
            else item.travel_from_previous_min * 60
        )
        if travel_seconds is None:
            self.errors.append(f"job {item.job_id} has no raw travel time")
        else:
            return math.ceil(travel_seconds / self.data.config.time_unit_seconds)
        return 0
