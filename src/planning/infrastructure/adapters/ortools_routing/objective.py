"""Сколько стоит вариант плана и почему важные цели нельзя обменять на пробег."""

import math

from planning.application.services.planning_input_normalizer import is_base_compatible
from planning.domain.entities.planning import PlanningInput
from planning.domain.enums import TransportType


def calculate_objective_ranges(
    data: PlanningInput,
    time_seconds_matrices: dict[str, list[list[int | None]]],
    distance_matrices: dict[str, list[list[int | None]]],
) -> dict[str, object]:
    """Рассчитать границы и веса, сохраняющие порядок целей из ТЗ, §8.4.

    Пропуск заявок → новые инженеры → общий пробег → наибольший пробег
    инженера → время дороги. Единица более важной цели дороже всей
    возможной суммы менее важных. Это оценка варианта, а не сам поиск.

    Короткие ключи nmax/emax/... в результате сохранены: их читает Validator
    и сохраняет история запусков. Локальные переменные расшифровывают их.
    """

    max_jobs = len(data.jobs)
    vehicle_ids = {engineer.id for engineer in data.engineers}
    max_engineers = len(data.engineers) + len(
        data.fixed_active_engineer_ids - vehicle_ids
    )
    max_arc_distance = max_arc_time = 0
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
        start_node = max_jobs + vehicle
        for destination in compatible:
            travel = time_matrix[start_node][destination]
            distance = distance_matrix[start_node][destination]
            if travel is not None and distance is not None:
                max_arc_distance = max(
                    max_arc_distance,
                    (distance + data.config.distance_unit_meters - 1)
                    // data.config.distance_unit_meters,
                )
                max_arc_time = max(
                    max_arc_time,
                    (travel + data.config.time_unit_seconds - 1)
                    // data.config.time_unit_seconds,
                )
        for source in compatible:
            for destination in compatible:
                if source == destination:
                    continue
                travel = time_matrix[source][destination]
                distance = distance_matrix[source][destination]
                if travel is not None and distance is not None:
                    max_arc_distance = max(
                        max_arc_distance,
                        (distance + data.config.distance_unit_meters - 1)
                        // data.config.distance_unit_meters,
                    )
                    max_arc_time = max(
                        max_arc_time,
                        (travel + data.config.time_unit_seconds - 1)
                        // data.config.time_unit_seconds,
                    )
    route_budgets = {
        str(engineer.id): math.ceil(
            max(0, engineer.shift_end_min - engineer.shift_start_min)
            * 60
            / data.config.time_unit_seconds
        )
        for engineer in data.engineers
    }
    max_total_distance = max_jobs * max_arc_distance
    max_route_distance = max_jobs * max_arc_distance + max(
        fixed_distance_units(data).values(), default=0
    )
    # При других единицах округление каждой дуги добавляет до одной единицы.
    # Для стандартных минут это уже учтено ограничениями Time.
    rounding_allowance = 0 if data.config.time_unit_seconds == 60 else max_jobs
    max_total_time = (
        min(max_jobs * max_arc_time, sum(route_budgets.values()) + rounding_allowance)
        if max_jobs
        else 0
    )
    # Общий делитель уменьшает числа, сохраняя сравнение любых сумм штрафов.
    # Обязательную заявку пропустить нельзя, поэтому её штраф сюда не входит.
    positive_drop_costs = [
        job.drop_penalty
        for job in data.jobs
        if not job.mandatory and job.drop_penalty > 0
    ]
    drop_cost_divisor = math.gcd(*positive_drop_costs) if positive_drop_costs else 1
    max_drop_cost = sum(
        max(0, job.drop_penalty) // drop_cost_divisor
        for job in data.jobs
        if not job.mandatory
    )
    # Веса строятся снизу вверх. +1 делает превосходство строгим.
    w_time = 1
    w_max_distance = max_total_time + 1
    w_total_distance = max_route_distance * w_max_distance + max_total_time + 1
    w_engineer = (
        max_total_distance * w_total_distance
        + max_route_distance * w_max_distance
        + max_total_time
        + 1
    )
    w_drop = (
        max_engineers * w_engineer
        + max_total_distance * w_total_distance
        + max_route_distance * w_max_distance
        + max_total_time
        + 1
    )
    lower_objective_maximum = (
        max_engineers * w_engineer
        + max_total_distance * w_total_distance
        + max_route_distance * w_max_distance
        + max_total_time
    )
    composite_maximum_objective = max_drop_cost * w_drop + lower_objective_maximum
    if not (
        w_max_distance > max_total_time
        and w_total_distance > max_route_distance * w_max_distance + max_total_time
        and w_engineer
        > max_total_distance * w_total_distance
        + max_route_distance * w_max_distance
        + max_total_time
        and w_drop
        > max_engineers * w_engineer
        + max_total_distance * w_total_distance
        + max_route_distance * w_max_distance
        + max_total_time
    ):
        raise RuntimeError("INVALID_OBJECTIVE_WEIGHTS")
    drop_priority_stages = list(data.snapshot.get("drop_priority_stages", []))
    staged_required = (
        data.snapshot.get("penalty_encoding") == "LEXICOGRAPHIC_STAGES_REQUIRED"
    )
    staged = staged_required or composite_maximum_objective >= 2**63 - 1
    if staged:
        # Без подготовленных бизнес-уровней нельзя безопасно угадать смысл
        # большого штрафа. Прямые вызовы solver по-прежнему завершаются ошибкой.
        if not drop_priority_stages:
            raise RuntimeError("OBJECTIVE_RANGE_OVERFLOW")
        _validate_drop_priority_stages(data, drop_priority_stages)
        stage_maxima = {
            str(stage["name"]): int(stage["maximum_objective"])
            for stage in drop_priority_stages
        }
        stage_maxima["ROUTE"] = lower_objective_maximum
        if max(stage_maxima.values(), default=0) >= 2**63 - 1:
            raise RuntimeError("OBJECTIVE_RANGE_OVERFLOW")
        maximum_objective = max(stage_maxima.values(), default=0)
    else:
        stage_maxima = {}
        maximum_objective = composite_maximum_objective
    return {
        "algorithm_version": "objective-range-v5" if staged else "objective-range-v3",
        "solve_strategy": (
            "LEXICOGRAPHIC_STAGES" if staged else "WEIGHTED_SINGLE_PASS"
        ),
        **(
            {
                "drop_priority_stages": drop_priority_stages,
                "stage_maximum_objectives": stage_maxima,
            }
            if staged
            else {}
        ),
        "distance_unit_meters": data.config.distance_unit_meters,
        "time_unit_seconds": data.config.time_unit_seconds,
        "nmax": max_jobs,
        "emax": max_engineers,
        "admax": max_arc_distance,
        "atmax": max_arc_time,
        "pmax": max_drop_cost,
        "drop_cost_divisor": drop_cost_divisor,
        "dmax": max_total_distance,
        "mmax": max_route_distance,
        "tmax": max_total_time,
        "route_time_budgets": route_budgets,
        "maximum_objective": maximum_objective,
        "composite_maximum_objective": composite_maximum_objective,
        "int64_fraction": maximum_objective / (2**63 - 1),
        "weights": {
            "w_time": w_time,
            "w_max_distance": w_max_distance,
            "w_total_distance": w_total_distance,
            "w_engineer": w_engineer,
            "w_drop": w_drop,
        },
    }


def _validate_drop_priority_stages(
    data: PlanningInput, stages: list[dict[str, object]]
) -> None:
    """Не передавать OR-Tools неполную или повреждённую иерархию пропусков."""
    group_order = {
        "OVERDUE": 0,
        "DUE_TODAY": 1,
        "DUE_IN_1_DAY": 2,
        "DUE_IN_2_3_DAYS": 3,
        "DUE_LATER_IN_CURRENT_BLOCK": 4,
        "RESERVE": 5,
    }
    optional_jobs = {job.id: job for job in data.jobs if not job.mandatory}
    described_ids: list[int] = []
    names: list[str] = []
    order_keys: list[tuple[int, int]] = []
    try:
        for stage in stages:
            group = str(stage["priority_group"])
            priority_type = str(stage["priority_type"])
            name = str(stage["name"])
            if priority_type not in {"EMERGENCY", "NORMAL"}:
                raise ValueError
            if name != f"{group}:{priority_type}":
                raise ValueError
            costs = {
                int(job_id): int(cost)
                for job_id, cost in dict(stage["job_costs"]).items()
            }
            count_weight = int(stage["count_weight"])
            if (
                count_weight <= 0
                or int(stage["secondary_divisor"]) <= 0
                or any(cost < count_weight for cost in costs.values())
                or int(stage["maximum_objective"]) != sum(costs.values())
            ):
                raise ValueError
            for job_id in costs:
                job = optional_jobs.get(job_id)
                if job is None:
                    raise ValueError
                actual_type = (
                    "EMERGENCY" if str(job.priority_type) == "EMERGENCY" else "NORMAL"
                )
                if actual_type != priority_type:
                    raise ValueError
            described_ids.extend(costs)
            names.append(name)
            order_keys.append(
                (group_order[group], 0 if priority_type == "EMERGENCY" else 1)
            )
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("OBJECTIVE_RANGE_OVERFLOW") from None
    if (
        len(names) != len(set(names))
        or order_keys != sorted(order_keys)
        or len(described_ids) != len(set(described_ids))
        or set(described_ids) != set(optional_jobs)
    ):
        raise RuntimeError("OBJECTIVE_RANGE_OVERFLOW")


def fixed_distance_units(data: PlanningInput) -> dict[int, int]:
    """Учесть фиксированный пробег, округляя каждый переезд отдельно."""
    return {
        engineer_id: sum(
            math.ceil(leg / data.config.distance_unit_meters) for leg in legs
        )
        for engineer_id, legs in data.fixed_distance_legs_by_engineer.items()
    }


def penalty_metrics(data: PlanningInput) -> dict[str, int]:
    components = data.snapshot.get("penalty_components", {})
    return {
        "original_daily_penalty_sum": sum(
            int(value.get("daily_drop_penalty_v2", 0)) for value in components.values()
        ),
        "emergency_bonus_sum": sum(
            int(value.get("emergency_bonus", 0)) for value in components.values()
        ),
    }
