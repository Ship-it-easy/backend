"""Reproducible scenario exports; run from backend with PYTHONPATH=src."""

import json
from datetime import date, timedelta
from pathlib import Path

from planning.domain.traffic import (
    CORRIDORS,
    INTERVAL_MINUTES,
    QUALITY,
    SLOTS_PER_DAY,
    SOURCES,
    VERSION,
    profile_table,
)

root = Path(__file__).resolve().parents[1]
tables = [profile_table(date(2026, 9, 21) + timedelta(days=i)) for i in range(7)]
profiles = []
for table in tables:
    groups = {}
    for row in table["rows"]:
        groups.setdefault((row["corridor_id"], row["direction"]), []).append(
            row["coefficient"]
        )
    profiles.extend(
        {
            "weekday": date.fromisoformat(table["date"]).weekday(),
            "corridor_id": key[0],
            "direction": key[1],
            "coefficients": values,
        }
        for key, values in groups.items()
    )
output = root / "data" / "traffic"
output.mkdir(exist_ok=True)
(output / "moscow_2026_profiles.json").write_text(
    json.dumps(
        {
            "version": VERSION,
            "quality": QUALITY,
            "timezone": "Europe/Moscow",
            "interval_minutes": INTERVAL_MINUTES,
            "sources": SOURCES,
            "profiles": profiles,
            "warning": (
                "Uncalibrated scenario, not measured 2026 traffic. "
                "No holiday or incident data."
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ),
    encoding="utf-8",
)
rows = tables[2]["rows"]
lines = [
    "# Таблица коэффициентов: Москва и южное Подмосковье, шаг 5 минут",
    "",
    "Сценарная оценка 2026.2, типичная среда, не наблюдения. "
    "Часовой пояс Europe/Moscow. "
    "Для радиальных дорог здесь усреднённое направление both; inbound/outbound "
    "и остальные дни недели доступны в JSON. Коэффициент = время / базовое время.",
    "",
    "| Время | " + " | ".join(v[0] for v in CORRIDORS.values()) + " |",
    "| --- | " + " | ".join("---:" for _ in CORRIDORS) + " |",
]
for slot in range(SLOTS_PER_DAY):
    clock = f"{slot * INTERVAL_MINUTES // 60:02d}:{slot * INTERVAL_MINUTES % 60:02d}"
    values = [
        next(
            r["coefficient"]
            for r in rows
            if r["corridor_id"] == key
            and r["direction"] == "both"
            and r["time"] == clock
        )
        for key in CORRIDORS
    ]
    lines.append("| " + clock + " | " + " | ".join(f"{v:.3f}" for v in values) + " |")
(root / "docs" / "TRAFFIC_TABLE_2026.md").write_text(
    "\n".join(lines) + "\n", encoding="utf-8"
)
print(
    f"Exported {len(profiles)} profiles, {len(profiles) * SLOTS_PER_DAY} coefficients"
)
