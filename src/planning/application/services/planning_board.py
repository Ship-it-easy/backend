from __future__ import annotations

import logging
from datetime import date
from typing import Any

from planning.domain.enums import WorkPriority
from planning.domain.priority import work_priority, work_priority_rank

REASON_TEXTS: dict[str, str] = {
    "OPTIMIZER_SELECTED": "Выбрано оптимизатором среди допустимых вариантов",
    "WORK_TYPE_PRIORITY": "Приоритет типа работ учтён при выборе заявки",
    "OVERDUE_PRIORITY": "Просроченная заявка получила повышенный приоритет",
    "SLA_DUE_TODAY": "Крайний срок сегодня",
    "HARD_CONSTRAINTS_MATCHED": ("Заявка соответствует смене и требованиям инженера"),
    "NO_ELIGIBLE_ENGINEER": ("Нет инженера, удовлетворяющего обязательным требованиям"),
    "NO_QUALIFICATION": "Нет инженера с требуемой квалификацией",
    "NO_EQUIPMENT": "Недостаточно доступного оборудования",
    "NO_SHIFT": "Нет доступной смены",
    "NO_TRANSPORT": "Нет подходящего транспорта",
    "ENGINEER_UNAVAILABLE": "Назначенный инженер стал недоступен",
    "TIME_WINDOW_CONFLICT": "Невозможно соблюсти временное окно",
    "SHIFT_CAPACITY_EXCEEDED": ("Работа не помещается в доступный остаток смены"),
    "ROUTE_INFEASIBLE": "Не найден допустимый маршрут с учётом дороги",
    "DROPPED_BY_OBJECTIVE": "Заявка не вошла в маршрут выбранного дня",
    "DATASET_LIMIT": "Заявка не вошла в лимит кандидатов расчёта",
    "GEOCODING_FAILED": "Не удалось определить координаты адреса заявки",
    "TIME_LIMIT_NO_ASSIGNMENT": "Расчёт завершён по лимиту времени",
    "HORIZON_EXHAUSTED": "Заявка не назначена до конца горизонта",
    "CANCELLED_RECORD": "Заявка отменена и исключена из активного маршрута",
    "INVALID_INPUT": "В заявке недостаточно данных для планирования",
    "MISSING_SERVICE_DURATION": "Не указана длительность работ",
    "NOT_ASSIGNED_IN_NEW_HORIZON": (
        "После пересчёта не найдено допустимое назначение в новом горизонте"
    ),
    "RESULT_DATA_UNAVAILABLE": "Сохранённый результат заявки неполон",
}

logger = logging.getLogger(__name__)


LEGACY_REASON_CODES: dict[str, str] = {
    "NO_AVAILABLE_ENGINEER": "NO_SHIFT",
    "NO_AVAILABLE_ENGINEER_TODAY": "NO_SHIFT",
    "NO_COMPATIBLE_ENGINEER": "NO_ELIGIBLE_ENGINEER",
    "NO_COMPATIBLE_ENGINEER_IN_HORIZON": "NO_ELIGIBLE_ENGINEER",
    "NO_SHIFT_IN_HORIZON": "NO_SHIFT",
    "DURATION_EXCEEDS_ALL_SHIFTS": "SHIFT_CAPACITY_EXCEEDED",
    "EQUIPMENT_UNAVAILABLE_IN_HORIZON": "NO_EQUIPMENT",
    "DAILY_EQUIPMENT_CAPACITY": "NO_EQUIPMENT",
    "INVALID_TIME_WINDOW": "TIME_WINDOW_CONFLICT",
    "INVALID_TIME_WINDOW_FOR_HORIZON": "TIME_WINDOW_CONFLICT",
    "DAILY_TIME_WINDOW_CONFLICT": "TIME_WINDOW_CONFLICT",
    "TRAVEL_DATA_NOT_READY": "ROUTE_INFEASIBLE",
    "DISTANCE_DATA_NOT_READY": "ROUTE_INFEASIBLE",
    "TRAVEL_PROVIDER_UNAVAILABLE": "ROUTE_INFEASIBLE",
    "NOT_SELECTED_BY_OPTIMIZER": "DROPPED_BY_OBJECTIVE",
    "NOT_ASSIGNED_WITHIN_HORIZON": "HORIZON_EXHAUSTED",
    "SLA_OUTSIDE_MAXIMUM_HORIZON": "HORIZON_EXHAUSTED",
    "CANCELLED_EN_ROUTE_ASSUMPTION": "CANCELLED_RECORD",
}


def reason(code: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = LEGACY_REASON_CODES.get(code, code)
    parameters = parameters or {}
    if normalized not in REASON_TEXTS:
        logger.error("unknown_planning_reason code=%s", code)
    return {
        "code": normalized,
        "text": _reason_text(normalized, parameters),
        "parameters": parameters,
        "known": normalized in REASON_TEXTS,
    }


def _reason_text(code: str, parameters: dict[str, Any]) -> str:
    if code == "NO_EQUIPMENT":
        missing = parameters.get("missing_equipment") or []
        labels = []
        for item in missing:
            if isinstance(item, dict):
                name = item.get("name") or f"оборудование #{item.get('id')}"
                units = item.get("available_units")
                labels.append(
                    f"{name} (доступно: {units})" if units is not None else str(name)
                )
            else:
                labels.append(f"оборудование #{item}")
        if labels:
            return "Недоступно обязательное оборудование: " + ", ".join(labels)
    return REASON_TEXTS.get(code, "Подробная причина недоступна")


def assigned_primary_reason(
    job: dict[str, Any], planning_date: date, eligible_engineers_count: int
) -> dict[str, Any]:
    priority = work_priority(job.get("priority", "LOW"))
    if priority != WorkPriority.LOW:
        return reason(
            "WORK_TYPE_PRIORITY",
            {
                "priority": priority.value,
                "priority_bonus": job.get("priority_bonus"),
            },
        )
    sla_date = _date(job.get("sla_date"))
    if sla_date is not None and sla_date < planning_date:
        return reason(
            "OVERDUE_PRIORITY",
            {"overdue_days": (planning_date - sla_date).days},
        )
    if sla_date == planning_date:
        return reason("SLA_DUE_TODAY", {"sla_date": sla_date.isoformat()})
    return reason(
        "OPTIMIZER_SELECTED",
        {"eligible_engineers_count": eligible_engineers_count},
    )


def unassigned_reason(
    saved_code: str | None,
    *,
    solver_status: str | None,
    diagnostic_flags: dict[str, Any] | None = None,
    final_horizon: bool = False,
) -> dict[str, Any]:
    # FEASIBLE_TIME_LIMIT describes the quality of the whole solver result,
    # not the proven cause of one dropped job. Keep it in technical diagnostics
    # and preserve the saved per-job reason shown to the dispatcher.
    if final_horizon and saved_code in {
        None,
        "NOT_ASSIGNED_WITHIN_HORIZON",
    }:
        code = "HORIZON_EXHAUSTED"
    else:
        code = saved_code or "DROPPED_BY_OBJECTIVE"
    return reason(code, diagnostic_flags or {})


def unassigned_sort_key(item: dict[str, Any], planning_date: date) -> tuple[Any, ...]:
    sla = _date(item.get("sla_date")) or date.max
    created = str(item.get("created_at") or "")
    return (
        0 if sla < planning_date else 1,
        work_priority_rank(item.get("priority", "LOW")),
        sla,
        created,
        int(item.get("job_id") or 0),
    )


def _date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
