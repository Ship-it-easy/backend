"""Повторный подсчёт стоимости по готовым маршрутам, независимо от OR-Tools.

Не импортируем формулы из infrastructure: иначе одна ошибка построителя могла бы
одновременно попасть в расчёт и в его проверку. Контракт метрик остаётся прежним.
"""

import math

from planning.domain.entities.planning import PlanningInput, PlanningResult


def check_costs_and_objective(
    data: PlanningInput,
    result: PlanningResult,
    actual_travel_time_units: int,
    errors: list[str],
) -> None:
    """Сверить отчётные стоимости, границы, число инженеров и итоговую оценку.

    Веса и границы берём из metadata результата, а фактические составляющие
    пересчитываем. Для старого результата без metadata действует прежняя сумма
    drop_cost + travel_cost. Оптимальность маршрута эта проверка не доказывает.
    """
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
        staged = metrics.get("solve_strategy") == "LEXICOGRAPHIC_STAGES"
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
        full_distance_units = {
            engineer_id: sum(
                math.ceil(leg / data.config.distance_unit_meters) for leg in legs
            )
            for engineer_id, legs in data.fixed_distance_legs_by_engineer.items()
        }
        for route, units in zip(result.routes, distance_units_by_route, strict=True):
            full_distance_units[route.engineer_id] = (
                full_distance_units.get(route.engineer_id, 0) + units
            )
        actual_max_distance = max(full_distance_units.values(), default=0)
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
                or (staged and metrics.get("objective_stage") != "ROUTE")
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
        expected_objective = model_drop * weights["w_drop"] + lower_objective
        if staged:
            expected_objective = _check_staged_objectives(
                data, result, lower_objective, errors
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


def _check_staged_objectives(
    data: PlanningInput,
    result: PlanningResult,
    lower_objective: int,
    errors: list[str],
) -> int:
    """Независимо пересчитать завершённые уровни по неназначенным job id."""
    metrics = result.objective_metrics
    stage_definitions = list(metrics.get("drop_priority_stages", []))
    snapshot_definitions = list(data.snapshot.get("drop_priority_stages", []))
    invalid_structure = False
    if not stage_definitions or stage_definitions != snapshot_definitions:
        errors.append("invalid drop priority stage definitions")
        invalid_structure = not stage_definitions
    optional_job_ids = {job.id for job in data.jobs if not job.mandatory}
    described_job_ids: list[int] = []
    stage_names: list[str] = []
    for definition in stage_definitions:
        name = str(definition.get("name"))
        stage_names.append(name)
        try:
            costs = {
                int(job_id): int(cost)
                for job_id, cost in definition["job_costs"].items()
            }
            valid_costs = all(cost > 0 for cost in costs.values())
            valid_maximum = int(definition["maximum_objective"]) == sum(costs.values())
            valid_encoding = (
                int(definition["count_weight"]) > 0
                and int(definition["secondary_divisor"]) > 0
            )
        except (KeyError, TypeError, ValueError):
            costs = {}
            valid_costs = valid_maximum = valid_encoding = False
            invalid_structure = True
        described_job_ids.extend(costs)
        if (
            not valid_costs
            or not valid_maximum
            or not valid_encoding
            or not set(costs).issubset(optional_job_ids)
        ):
            errors.append(f"invalid drop priority stage: {name}")
    if (
        len(stage_names) != len(set(stage_names))
        or len(described_job_ids) != len(set(described_job_ids))
        or set(described_job_ids) != optional_job_ids
    ):
        errors.append("invalid drop priority stage coverage")
        invalid_structure = True
    if invalid_structure:
        return result.objective
    definitions = {str(stage["name"]): stage for stage in stage_definitions}
    model_job_ids = {job.id for job in data.jobs}
    dropped_ids = {
        item.job_id for item in result.unassigned if item.job_id in model_job_ids
    }
    actual = {
        name: sum(
            int(cost)
            for job_id, cost in stage["job_costs"].items()
            if int(job_id) in dropped_ids
        )
        for name, stage in definitions.items()
    }
    records = list(metrics.get("stages", []))
    completed_names = [str(item.get("name")) for item in records]
    expected_order = [str(stage["name"]) for stage in stage_definitions]
    if completed_names and completed_names[-1] == "ROUTE":
        expected_prefix = [*expected_order, "ROUTE"]
    else:
        expected_prefix = expected_order[: len(completed_names)]
    if completed_names != expected_prefix:
        errors.append("invalid objective stages")
    complete = completed_names == [*expected_order, "ROUTE"]
    if bool(metrics.get("all_stages_completed")) != complete:
        errors.append("invalid all stages completed flag")
    for record in records:
        name = str(record.get("name"))
        expected = lower_objective if name == "ROUTE" else actual.get(name)
        if expected is None or record.get("objective") != expected:
            errors.append(f"invalid stage objective: {name}")
        maximum = metrics.get("stage_maximum_objectives", {}).get(name)
        if maximum is None or (expected is not None and expected > maximum):
            errors.append(f"actual stage objective exceeds proven maximum: {name}")
    locked = metrics.get("locked_drop_objectives", {})
    expected_locked_names = [name for name in completed_names if name != "ROUTE"]
    if list(locked) != expected_locked_names:
        errors.append("invalid locked objective stages")
    for name, value in locked.items():
        if name not in actual or actual[name] != value:
            errors.append(f"locked drop objective differs from result: {name}")
    stage = metrics.get("objective_stage")
    expected_objective = lower_objective if stage == "ROUTE" else actual.get(stage)
    if expected_objective is None:
        errors.append("invalid current objective stage")
        return result.objective
    expected_weights = dict(weights := metrics["weights"])
    if stage == "ROUTE":
        expected_weights["w_drop"] = 0
    else:
        expected_weights = {name: 0 for name in weights}
    if metrics.get("stage_weights") != expected_weights:
        errors.append("invalid stage weights")
    return expected_objective
