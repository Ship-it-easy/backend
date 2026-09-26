"""Перевод правил одного дня в ограничения RoutingModel.

Читайте build() сверху вниз. Node — номер точки во входных списках;
index — внутренний номер OR-Tools; vehicle — позиция инженера, не его ID в БД.
"""

import math

from ortools.constraint_solver import pywrapcp

from planning.application.errors import SolverNoFeasibleSolution
from planning.application.services.planning_input_normalizer import is_base_compatible
from planning.domain.entities.planning import PlanningInput
from planning.domain.enums import TransportType

from .objective import calculate_objective_ranges, fixed_distance_units
from .travel import RoutingMatrices

# Служебные границы прежней минутной модели, не бизнес-настройки смены.
BLOCKED_MINUTES = 100_000
MAX_WAIT_MINUTES = 1440
TIME_CAPACITY_MINUTES = 2880


class DailyRoutingModel:
    """Одна модель на один solve; объект не переиспользуется между расчётами."""

    def __init__(
        self,
        data: PlanningInput,
        travel: RoutingMatrices,
        *,
        objective_stage: str | None = None,
        locked_drop_objectives: dict[str, int] | None = None,
    ):
        self.data = data
        self.matrices = travel.minutes
        self.seconds_matrices = travel.seconds
        self.distance_matrices = travel.meters
        self.job_count, self.vehicle_count = (
            len(self.data.jobs),
            len(self.data.engineers),
        )
        self.prefix_units = fixed_distance_units(self.data)
        self.outside_prefix = max(self.prefix_units.values(), default=0)
        # Служебный пустой маршрут хранит максимум уже фиксированного пробега.
        # Так Distance учитывает весь день, включая инженеров вне этой модели.
        if self.outside_prefix:
            self.vehicle_count += 1
        self.end_node = self.job_count + self.vehicle_count
        starts = [self.job_count + vehicle for vehicle in range(self.vehicle_count)]
        self.manager = pywrapcp.RoutingIndexManager(
            self.end_node + 1,
            self.vehicle_count,
            starts,
            [self.end_node] * self.vehicle_count,
        )
        self.routing = pywrapcp.RoutingModel(self.manager)
        self.objective_ranges = calculate_objective_ranges(
            self.data, self.seconds_matrices, self.distance_matrices
        )
        self.drop_priority_stages = list(
            self.objective_ranges.get("drop_priority_stages", [])
        )
        self.objective_stage = objective_stage or (
            str(self.drop_priority_stages[0]["name"])
            if self.objective_ranges["solve_strategy"] == "LEXICOGRAPHIC_STAGES"
            else "COMBINED"
        )
        self.locked_drop_objectives = dict(locked_drop_objectives or {})
        self.current_drop_stage = next(
            (
                stage
                for stage in self.drop_priority_stages
                if stage["name"] == self.objective_stage
            ),
            None,
        )
        if self.objective_stage not in {"COMBINED", "ROUTE"} and not (
            self.current_drop_stage
        ):
            raise ValueError(f"Unknown objective stage: {self.objective_stage}")
        self.weights = dict(self.objective_ranges["weights"])
        if self.current_drop_stage is not None:
            self.weights = {name: 0 for name in self.weights}
        elif self.objective_stage == "ROUTE":
            self.weights["w_drop"] = 0

    def build(self) -> None:
        """Добавить правила в прежнем порядке: он влияет на эвристический поиск."""
        self._register_travel_callbacks()
        self._add_time_and_distance_dimensions()
        self._set_engineer_shifts()
        self._set_job_windows_and_allowed_engineers()
        self._forbid_missing_arcs()
        self._forbid_time_infeasible_arcs()
        self._add_equipment_constraints()
        self._lock_completed_drop_objectives()

    def _register_travel_callbacks(self) -> None:
        """Задать длительность, стоимость и пробег перехода для каждого инженера.

        Callback вызывается OR-Tools для пары точек. Значения по умолчанию
        у вложенных функций фиксируют профиль текущего инженера в цикле.
        """
        self.transit_callbacks: list[int] = []
        self.distance_callbacks: list[int] = []
        weights = self.weights
        for vehicle, engineer in enumerate(self.data.engineers):
            profile = TransportType(engineer.transport_type).routing_profile
            matrix = self.matrices[profile]
            seconds_matrix = self.seconds_matrices[profile]
            distance_matrix = self.distance_matrices[profile]

            def elapsed_minutes(from_index: int, to_index: int, matrix=matrix) -> int:
                from_node = self.manager.IndexToNode(from_index)
                to_node = self.manager.IndexToNode(to_index)
                service = (
                    self.data.jobs[from_node].duration_min
                    if from_node < self.job_count
                    else 0
                )
                if to_node == self.end_node:
                    # Обратного пути нет. Но последняя работа всё равно должна
                    # завершиться в смену: её длительность оставляем в переходе.
                    return service
                if from_node >= len(matrix) or to_node >= len(matrix):
                    return BLOCKED_MINUTES
                travel = matrix[from_node][to_node]
                return service + (BLOCKED_MINUTES if travel is None else travel)

            def travel_cost(
                from_index,
                to_index,
                seconds_matrix=seconds_matrix,
                distance_matrix=distance_matrix,
            ):
                from_node = self.manager.IndexToNode(from_index)
                to_node = self.manager.IndexToNode(to_index)
                if to_node == self.end_node:
                    return 0
                if from_node >= len(seconds_matrix) or to_node >= len(seconds_matrix):
                    return 0
                travel_seconds = seconds_matrix[from_node][to_node]
                distance = distance_matrix[from_node][to_node]
                if travel_seconds is None or distance is None:
                    return 0  # Недоступный переезд будет явно запрещён ограничением.
                distance_units = math.ceil(
                    distance / self.data.config.distance_unit_meters
                )
                time_units = math.ceil(
                    travel_seconds / self.data.config.time_unit_seconds
                )
                return (
                    distance_units * weights["w_total_distance"]
                    + time_units * weights["w_time"]
                )

            def distance_units(
                from_index,
                to_index,
                distance_matrix=distance_matrix,
                prefix=self.prefix_units.get(engineer.id, 0),
            ):
                from_node = self.manager.IndexToNode(from_index)
                to_node = self.manager.IndexToNode(to_index)
                fixed = prefix if self.routing.IsStart(from_index) else 0
                if to_node == self.end_node:
                    return fixed
                if from_node >= len(distance_matrix) or to_node >= len(distance_matrix):
                    return fixed
                value = distance_matrix[from_node][to_node]
                return fixed + (
                    0
                    if value is None
                    else math.ceil(value / self.data.config.distance_unit_meters)
                )

            transit_index = self.routing.RegisterTransitCallback(elapsed_minutes)
            cost_index = self.routing.RegisterTransitCallback(travel_cost)
            distance_index = self.routing.RegisterTransitCallback(distance_units)
            self.transit_callbacks.append(transit_index)
            self.distance_callbacks.append(distance_index)
            self.routing.SetArcCostEvaluatorOfVehicle(cost_index, vehicle)
            fixed_cost = 0
            if self.current_drop_stage is None:
                fixed_cost = (
                    0
                    if engineer.id in self.data.fixed_active_engineer_ids
                    else weights["w_engineer"]
                )
            self.routing.SetFixedCostOfVehicle(fixed_cost, vehicle)

        if self.outside_prefix:
            # Этот маршрут не получает заявок. Он нужен только для нижней
            # границы максимального пробега; не создаёт нового инженера в отчёте.
            fixed_distance_vehicle = self.vehicle_count - 1
            zero = self.routing.RegisterTransitCallback(lambda _from, _to: 0)
            prefix = self.routing.RegisterTransitCallback(
                lambda _from, _to: self.outside_prefix
            )
            self.transit_callbacks.append(zero)
            self.distance_callbacks.append(prefix)
            self.routing.SetArcCostEvaluatorOfVehicle(zero, fixed_distance_vehicle)
            self.routing.NextVar(self.routing.Start(fixed_distance_vehicle)).SetValue(
                self.routing.End(fixed_distance_vehicle)
            )
            self.routing.SetVehicleUsedWhenEmpty(True, fixed_distance_vehicle)

    def _add_time_and_distance_dimensions(self) -> None:
        """Time накапливает время с ожиданием; Distance — пробег без ожидания."""
        self.routing.AddDimensionWithVehicleTransits(
            self.transit_callbacks,
            MAX_WAIT_MINUTES,
            TIME_CAPACITY_MINUTES,
            False,
            "Time",
        )
        self.time_dimension = self.routing.GetDimensionOrDie("Time")
        self.routing.AddDimensionWithVehicleTransits(
            self.distance_callbacks,
            0,
            self.objective_ranges["mmax"],
            True,
            "Distance",
        )
        distance_dimension = self.routing.GetDimensionOrDie("Distance")
        # У всех маршрутов Distance начинается с нуля, поэтому global span
        # равен максимальному конечному пробегу (включая фиксированную часть).
        distance_dimension.SetGlobalSpanCostCoefficient(self.weights["w_max_distance"])
        if self.outside_prefix:
            self.time_dimension.CumulVar(
                self.routing.Start(self.vehicle_count - 1)
            ).SetValue(0)
            self.time_dimension.CumulVar(
                self.routing.End(self.vehicle_count - 1)
            ).SetValue(0)

    def _set_engineer_shifts(self) -> None:
        """Начать с доступной границы смены, завершить все работы до её конца."""
        for vehicle, engineer in enumerate(self.data.engineers):
            earliest = engineer.shift_start_min
            # Публикуем самое раннее расписание от доступного начала смены.
            # Модель должна проверять маршрут от той же границы времени.
            self.time_dimension.CumulVar(self.routing.Start(vehicle)).SetValue(earliest)
            self.time_dimension.CumulVar(self.routing.End(vehicle)).SetRange(
                earliest, engineer.shift_end_min
            )

    def _set_job_windows_and_allowed_engineers(self) -> None:
        """Ограничить начало работы, исполнителя и возможность пропустить заявку.

        -1 в VehicleVar означает пропуск. Mandatory-заявка такого варианта
        не имеет; обычная получает штраф через AddDisjunction.
        """
        self.compatible_vehicles: dict[int, list[int]] = {}
        for job_index, job in enumerate(self.data.jobs):
            index = self.manager.NodeToIndex(job_index)
            compatible = [
                vehicle
                for vehicle, engineer in enumerate(self.data.engineers)
                if is_base_compatible(job, engineer)
                and engineer.shift_start_min <= job.window_end_min
                and engineer.shift_end_min - job.duration_min >= job.window_start_min
            ]
            self.compatible_vehicles[job.id] = compatible
            if job.mandatory and not compatible:
                raise SolverNoFeasibleSolution(
                    "Mandatory job has no compatible engineer"
                )
            latest_start = (
                max(
                    self.data.engineers[vehicle].shift_end_min - job.duration_min
                    for vehicle in compatible
                )
                if compatible
                else job.window_end_min
            )
            self.time_dimension.CumulVar(index).SetRange(
                job.window_start_min,
                min(job.window_end_min, latest_start)
                if compatible
                else job.window_end_min,
            )
            self.routing.VehicleVar(index).SetValues(
                compatible if job.mandatory else compatible + [-1]
            )
            if not job.mandatory:
                if self.objective_stage == "COMBINED":
                    business_drop_cost = (
                        job.drop_penalty // self.objective_ranges["drop_cost_divisor"]
                    )
                    disjunction_penalty = business_drop_cost * self.weights["w_drop"]
                elif self.current_drop_stage is not None:
                    disjunction_penalty = int(
                        self.current_drop_stage["job_costs"].get(str(job.id), 0)
                    )
                else:
                    # Все стоимости пропусков уже зафиксированы равенствами.
                    disjunction_penalty = 0
                self.routing.AddDisjunction([index], disjunction_penalty)
            # Если выбран этот инженер, работа обязана закончиться в его смену.
            # Иначе добавка снимает именно это персональное ограничение.
            for vehicle in compatible:
                not_assigned_to_vehicle = self.routing.solver().IsDifferentCstVar(
                    self.routing.VehicleVar(index), vehicle
                )
                self.routing.solver().Add(
                    self.time_dimension.CumulVar(index)
                    <= self.data.engineers[vehicle].shift_end_min
                    - job.duration_min
                    + TIME_CAPACITY_MINUTES * not_assigned_to_vehicle
                )

    def _forbid_missing_arcs(self) -> None:
        """Запретить переезды, для которых провайдер не нашёл дороги."""
        for vehicle, engineer in enumerate(self.data.engineers):
            profile = TransportType(engineer.transport_type).routing_profile
            matrix = self.matrices[profile]
            distance_matrix = self.distance_matrices[profile]
            start_index = self.routing.Start(vehicle)
            start_node = self.job_count + vehicle
            for to_node in range(self.job_count):
                to_index = self.manager.NodeToIndex(to_node)
                if (
                    matrix[start_node][to_node] is None
                    or distance_matrix[start_node][to_node] is None
                ):
                    self.routing.NextVar(start_index).RemoveValue(to_index)
            for from_node in range(self.job_count):
                from_index = self.manager.NodeToIndex(from_node)
                for to_node in range(self.job_count):
                    if from_node == to_node or (
                        matrix[from_node][to_node] is not None
                        and distance_matrix[from_node][to_node] is not None
                    ):
                        continue
                    to_index = self.manager.NodeToIndex(to_node)
                    self._forbid_arc_for_vehicle(from_index, to_index, vehicle)

    def _forbid_time_infeasible_arcs(self) -> None:
        """Заранее исключить переезды, заведомо нарушающие окно или смену.

        Здесь проверяем только пару точек. Выполнимость полной цепочки
        по-прежнему проверяет Time dimension во время поиска.
        """
        for vehicle, engineer in enumerate(self.data.engineers):
            profile = TransportType(engineer.transport_type).routing_profile
            matrix = self.matrices[profile]
            start_index = self.routing.Start(vehicle)
            start_node = self.job_count + vehicle
            for to_node, destination in enumerate(self.data.jobs):
                travel = matrix[start_node][to_node]
                if travel is None:
                    continue
                arrival = engineer.shift_start_min + travel
                start = max(arrival, destination.window_start_min)
                if (
                    start > destination.window_end_min
                    or start + destination.duration_min > engineer.shift_end_min
                ):
                    self.routing.NextVar(start_index).RemoveValue(
                        self.manager.NodeToIndex(to_node)
                    )

            for from_node, source in enumerate(self.data.jobs):
                from_index = self.manager.NodeToIndex(from_node)
                earliest_source_start = max(
                    engineer.shift_start_min, source.window_start_min
                )
                for to_node, destination in enumerate(self.data.jobs):
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
                    to_index = self.manager.NodeToIndex(to_node)
                    self._forbid_arc_for_vehicle(from_index, to_index, vehicle)

    def _lock_completed_drop_objectives(self) -> None:
        """Не позволить младшей цели ухудшить уже найденную старшую цель."""
        if not self.locked_drop_objectives:
            return
        optional_indices = {
            job.id: self.manager.NodeToIndex(index)
            for index, job in enumerate(self.data.jobs)
            if not job.mandatory
        }
        solver = self.routing.solver()
        stages = {str(stage["name"]): stage for stage in self.drop_priority_stages}
        for name, expected in self.locked_drop_objectives.items():
            stage = stages.get(name)
            if stage is None:
                raise ValueError(f"Unknown locked objective stage: {name}")
            terms = [
                int(cost) * (1 - self.routing.ActiveVar(optional_indices[int(job_id)]))
                for job_id, cost in stage["job_costs"].items()
                if int(job_id) in optional_indices
            ]
            solver.Add(solver.Sum(terms) == expected)

    def _forbid_arc_for_vehicle(
        self, from_index: int, to_index: int, vehicle: int
    ) -> None:
        """Запретить A → B именно этому инженеру; другим он может подходить.

        Нельзя одновременно: A у инженера, B у того же инженера, B сразу за A.
        Поэтому хотя бы одно из трёх условий «не равно» должно быть истинно.
        """
        solver = self.routing.solver()
        from_not_vehicle = solver.IsDifferentCstVar(
            self.routing.VehicleVar(from_index), vehicle
        )
        to_not_vehicle = solver.IsDifferentCstVar(
            self.routing.VehicleVar(to_index), vehicle
        )
        not_successor = solver.IsDifferentCstVar(
            self.routing.NextVar(from_index), to_index
        )
        solver.Add(from_not_vehicle + to_not_vehicle + not_successor >= 1)

    def _add_equipment_constraints(self) -> None:
        """Одна единица типа оборудования на инженера, а не на каждую заявку.

        uses = 1 тогда и только тогда, когда у инженера есть хотя бы одна
        работа с этим оборудованием. Уже выданные единицы учитываем отдельно.
        """
        solver = self.routing.solver()
        equipment_ids = {
            equipment_id
            for job in self.data.jobs
            for equipment_id in job.required_equipment
        }
        for equipment_id in equipment_ids:
            newly_allocated_variables = []
            for vehicle in range(len(self.data.engineers)):
                uses = solver.BoolVar(f"uses_equipment_{vehicle}_{equipment_id}")
                assigned_variables = []
                for job_index, job in enumerate(self.data.jobs):
                    if (
                        equipment_id not in job.required_equipment
                        or vehicle not in self.compatible_vehicles[job.id]
                    ):
                        continue
                    assigned = solver.IsEqualCstVar(
                        self.routing.VehicleVar(self.manager.NodeToIndex(job_index)),
                        vehicle,
                    )
                    assigned_variables.append(assigned)
                    # Назначенная работа требует выдать инженеру оборудование.
                    solver.Add(assigned <= uses)
                if assigned_variables:
                    # Без соответствующих работ резервировать единицу нельзя.
                    solver.Add(uses <= solver.Sum(assigned_variables))
                else:
                    solver.Add(uses == 0)
                engineer_id = self.data.engineers[vehicle].id
                if equipment_id not in self.data.preallocated_equipment_by_engineer.get(
                    engineer_id, frozenset()
                ):
                    newly_allocated_variables.append(uses)
            already_allocated = sum(
                equipment_id in values
                for values in self.data.preallocated_equipment_by_engineer.values()
            )
            remaining_units = max(
                0, self.data.equipment_units.get(equipment_id, 0) - already_allocated
            )
            if newly_allocated_variables:
                solver.Add(solver.Sum(newly_allocated_variables) <= remaining_units)
