"""Независимая проверка готового дневного плана перед его сохранением.

Валидатор не ищет лучший маршрут: сверяет конкретный PlanningResult с тем же
PlanningInput. Пустой список означает отсутствие обнаруженных нарушений.
"""

from collections import Counter

from planning.domain.entities.engineer import Engineer
from planning.domain.entities.planning import PlanningInput, PlanningResult
from planning.domain.enums import TransportType

from .planning_result_objective import check_costs_and_objective
from .planning_result_routes import RouteChecks


class PlanningValidator:
    def validate(self, data: PlanningInput, result: PlanningResult) -> list[str]:
        """Проверить состав → матрицы → маршруты/оборудование → стоимость.

        Сохраняем порядок и тексты ошибок. При повреждённых матрицах выходим
        до обращения к их ячейкам. Вход и результат не изменяем.
        """
        errors: list[str] = []
        jobs = {job.id: job for job in data.jobs}
        engineers = {engineer.id: engineer for engineer in data.engineers}
        _check_job_coverage(data, result, errors)
        _check_unique_engineer_routes(result, errors)
        _check_matrices(data, result, engineers, errors)
        if any("matrix" in error for error in errors):
            return errors

        # Отдельный объект на вызов: переиспользуемый валидатор не хранит состояние.
        travel_time_units = RouteChecks(
            data, result, jobs, engineers, errors
        ).validate()
        check_costs_and_objective(data, result, travel_time_units, errors)
        return errors


def _check_job_coverage(
    data: PlanningInput, result: PlanningResult, errors: list[str]
) -> None:
    """Каждая входная заявка должна иметь один исход; обязательная — назначение."""
    assigned = [item.job_id for route in result.routes for item in route.jobs]
    unassigned = [item.job_id for item in result.unassigned]
    expected_job_ids = {job.id for job in data.jobs} | {
        item.job_id for item in data.pre_unassigned
    }
    duplicates = [job_id for job_id, count in Counter(assigned).items() if count > 1]
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


def _check_unique_engineer_routes(result: PlanningResult, errors: list[str]) -> None:
    """На одного инженера приходится не больше одного маршрута дня."""
    repeated_engineers = [
        key
        for key, count in Counter(route.engineer_id for route in result.routes).items()
        if count > 1
    ]
    if repeated_engineers:
        errors.append(f"engineers have multiple routes: {repeated_engineers}")


def _check_matrices(
    data: PlanningInput,
    result: PlanningResult,
    engineers: dict[int, Engineer],
    errors: list[str],
) -> None:
    """Проверить размеры и значения матриц используемых транспортных профилей.

    None допустим как обозначение отсутствующей дороги. Выбор именно такой
    дороги проверяется далее при проходе по маршруту.
    """
    expected_size = len(data.jobs) + len(data.engineers)
    for profile in {
        TransportType(engineers[route.engineer_id].transport_type).routing_profile
        for route in result.routes
        if route.engineer_id in engineers
    }:
        for name, matrices in (
            ("minutes", result.travel_matrices),
            ("seconds", result.travel_time_seconds_matrices),
            ("distance", result.distance_matrices),
        ):
            matrix = matrices.get(profile)
            if (
                matrix is None
                or len(matrix) != expected_size
                or any(len(row) != expected_size for row in matrix)
            ):
                errors.append(f"missing or invalid {name} matrix for {profile}")
            elif any(
                value is not None and (type(value) is not int or value < 0)
                for row in matrix
                for value in row
            ):
                errors.append(f"invalid {name} matrix value for {profile}")
