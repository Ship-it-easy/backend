"""Исследование нормализации штрафов на одном обезличенном наборе из 16 заявок.

Не используется рабочим планировщиком и не рассчитывает маршруты. Полный перебор
подмножеств нужен только для проверки гипотезы; на рабочих объёмах не применим.
Запуск из корня: .venv/bin/python docs/refactoring/penalty_compression_spike.py
"""

import json
from pathlib import Path

from ortools.sat.python import cp_model


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    fixture = json.loads(
        (root / "tests/fixtures/planning_objective_overflow.json").read_text()
    )
    penalties = [row["drop_penalty"] for row in fixture["jobs"]]
    assert len(penalties) == 16, "This spike is limited to the saved 16-job case"
    # Вес из исходных границ этого snapshot, без изменения дорог и точности.
    w_drop = 52_860_199_185_860

    # Для каждого возможного набора пропусков запоминаем сумму и битовую маску.
    subsets = [(0, 0)]
    for index, penalty in enumerate(penalties):
        subsets += [
            (total + penalty, mask | (1 << index)) for total, mask in subsets[:]
        ]
    subsets.sort()

    # Равные суммы должны остаться равными; соседние различные суммы — строго
    # упорядоченными. По транзитивности это сохраняет все попарные сравнения.
    constraints = set()
    for (before, mask_a), (after, mask_b) in zip(subsets, subsets[1:], strict=False):
        coefficients = tuple(
            ((mask_b >> index) & 1) - ((mask_a >> index) & 1)
            for index in range(len(penalties))
        )
        constraints.add((coefficients, int(after > before)))

    model = cp_model.CpModel()
    weights = [
        model.new_int_var(1, sum(penalties), f"penalty_{index}")
        for index in range(len(penalties))
    ]
    for coefficients, strict in sorted(constraints):
        expression = sum(
            coefficient * weight
            for coefficient, weight in zip(coefficients, weights, strict=True)
            if coefficient
        )
        model.add(expression >= 1 if strict else expression == 0)
    model.minimize(sum(weights))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10
    solver.parameters.num_search_workers = 1
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise RuntimeError(f"No verified normalization: {solver.status_name(status)}")

    normalized = [solver.value(weight) for weight in weights]
    encoded = [
        sum(value for index, value in enumerate(normalized) if mask & (1 << index))
        for _, mask in subsets
    ]
    # Отдельная целочисленная проверка найденных значений без доверия к solver.
    for (before, _), (after, _), new_before, new_after in zip(
        subsets, subsets[1:], encoded, encoded[1:], strict=False
    ):
        assert (before == after) == (new_before == new_after)
        assert (before < after) == (new_before < new_after)

    result = {
        "scope": "saved 16-job fixture only; not production routing",
        "status": solver.status_name(status),
        "subset_count": len(subsets),
        "constraint_count": len(constraints),
        "original_pmax": sum(penalties),
        "normalized_penalties": normalized,
        "normalized_pmax": sum(normalized),
        "verified_all_subset_order_and_ties": True,
        "normalized_objective_bound": (sum(normalized) + 1) * w_drop - 1,
        "int64_max": 2**63 - 1,
    }
    output = Path(__file__).with_name("overflow-penalty-compression.json")
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
