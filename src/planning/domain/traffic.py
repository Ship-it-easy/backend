"""Versioned Moscow scenario, not measured or live traffic.

Published counts justify corridor selection only. Numerical speed profiles are
explicit priors; they must be calibrated against travel observations.
"""

import math
import re
from datetime import datetime, timedelta, timezone

MOSCOW = timezone(timedelta(hours=3))
VERSION = "moscow-south-scenario-2026.2-5min"
INTERVAL_MINUTES = 5
SLOTS_PER_DAY = 24 * 60 // INTERVAL_MINUTES
SERVICE_BOUNDS = (54.65, 36.85, 56.15, 38.65)
QUALITY = "uncalibrated_estimate"
SOURCES = [
    "https://t.mos.ru/mostrans/all_news/126798",
    "https://msd.mos.ru/faq",
    "https://i.transport.mos.ru/flyover/zagruzhennost_dorog_v_mae",
]
# Morning/evening amplitudes above uncongested time: assumptions, not observations.
CORRIDORS = {
    "mkad": ("МКАД", ("мкад",), 1.00, 1.30),
    "ttk": ("Третье транспортное кольцо", ("ттк", "третье транспортное"), 1.20, 1.50),
    "garden": (
        "Садовое кольцо",
        (
            "садовое кольцо",
            "садовая-",
            "земляной вал",
            "зацепский вал",
            "коровий вал",
            "крымский вал",
            "зубовский бульвар",
            "новинский бульвар",
            "смоленский бульвар",
            "смоленская-сенная",
        ),
        1.10,
        1.40,
    ),
    "msd": (
        "Московский скоростной диаметр",
        (
            "мсд",
            "московский скоростной",
            "северо-восточная хорда",
            "юго-восточная хорда",
        ),
        0.55,
        0.70,
    ),
    "leningrad": (
        "Ленинградское шоссе / проспект",
        ("ленинградское", "ленинградский"),
        1.20,
        1.45,
    ),
    "dmitrov": ("Дмитровское шоссе", ("дмитровское",), 1.10, 1.35),
    "yaroslav": (
        "Ярославское шоссе / проспект Мира",
        ("ярославское", "проспект мира"),
        1.15,
        1.40,
    ),
    "enthusiasts": ("Шоссе Энтузиастов", ("шоссе энтузиастов",), 1.25, 1.50),
    "volgograd": ("Волгоградский проспект", ("волгоградский",), 1.20, 1.45),
    "kashira": ("Каширское шоссе", ("каширское",), 1.05, 1.30),
    "warsaw": ("Варшавское шоссе", ("варшавское",), 1.05, 1.30),
    "lenin": ("Ленинский проспект", ("ленинский проспект",), 1.00, 1.25),
    "kutuzov": (
        "Кутузовский проспект / Можайское шоссе",
        ("кутузовский", "можайское"),
        1.10,
        1.35,
    ),
    "volokolam": ("Волоколамское шоссе", ("волоколамское",), 1.20, 1.45),
    "ryazan": (
        "Рязанский проспект / Новорязанское шоссе",
        ("рязанский проспект", "новорязанское"),
        1.15,
        1.40,
    ),
    "m4": ("М-4 Дон (южное Подмосковье)", ("м-4", "м4", "дон"), 0.45, 0.65),
    "m2": (
        "М-2 Крым / Симферопольское шоссе",
        ("м-2", "м2", "симферопольское шоссе", "крым"),
        0.45,
        0.60,
    ),
    "a107": (
        "А-107 Московское малое кольцо",
        ("а-107", "а107", "московское малое кольцо"),
        0.40,
        0.55,
    ),
    "a108": (
        "А-108 Московское большое кольцо",
        ("а-108", "а108", "московское большое кольцо"),
        0.30,
        0.45,
    ),
    "domodedovo": ("Улицы Домодедово (общая оценка)", (), 0.50, 0.65),
    "stupino": ("Улицы Ступино (общая оценка)", (), 0.35, 0.45),
    "kashira_local": ("Улицы Каширы (общая оценка)", (), 0.30, 0.40),
    "regional": ("Прочие дороги южного Подмосковья (общая оценка)", (), 0.25, 0.40),
    "urban": ("Остальные улицы Москвы (общая оценка)", (), 0.80, 1.00),
}

UNDIRECTED = {
    "mkad",
    "ttk",
    "garden",
    "msd",
    "a107",
    "a108",
    "urban",
    "regional",
    "domodedovo",
    "stupino",
    "kashira_local",
}


def covered(lat: float, lon: float) -> bool:
    south, west, north, east = SERVICE_BOUNDS
    return south <= lat <= north and west <= lon <= east


def area_profile(lat: float, lon: float) -> str | None:
    if not covered(lat, lon):
        return None
    if 55.50 <= lat <= 55.96 and 37.30 <= lon <= 37.95:
        return "urban"
    if 55.25 <= lat <= 55.52 and 37.60 <= lon <= 37.95:
        return "domodedovo"
    if 54.85 <= lat <= 55.00 and 37.95 <= lon <= 38.18:
        return "stupino"
    if 54.75 <= lat <= 54.90 and 38.10 <= lon <= 38.35:
        return "kashira_local"
    return "regional"


def corridor_for(names: list[str], points: list[list[float]]) -> str | None:
    if not points or not all(covered(*point) for point in points):
        return None
    name = " ".join(names).casefold().replace("ё", "е")
    middle = points[len(points) // 2]
    return next(
        (
            key
            for key, (_, aliases, _, _) in CORRIDORS.items()
            if any(
                re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", name)
                if len(alias) <= 5
                else alias in name
                for alias in aliases
            )
        ),
        area_profile(*middle),
    )


def direction_for(points: list[list[float]], corridor: str | None) -> str:
    if len(points) < 2 or corridor in UNDIRECTED:
        return "both"

    def radius(point):
        return (point[0] - 55.751) ** 2 + ((point[1] - 37.618) * 0.56) ** 2

    difference = radius(points[-1]) - radius(points[0])
    return (
        "both"
        if abs(difference) < 0.00001
        else ("outbound" if difference > 0 else "inbound")
    )


def coefficient(corridor: str, at: datetime, direction: str = "both") -> float:
    if at.utcoffset() is None:
        raise ValueError("Departure must contain a UTC offset")
    local = at.astimezone(MOSCOW)
    hour = (local.hour * 60 + local.minute // INTERVAL_MINUTES * INTERVAL_MINUTES) / 60
    _, _, am, pm = CORRIDORS[corridor]
    if local.weekday() >= 5:
        # Retain regional differences on weekends as well as weekdays.
        excess = 0.05 + min(0.40, (am + pm) * 0.20) * math.exp(
            -(((hour - 14) / 4) ** 2)
        )
    else:
        am *= 0.55 if direction == "outbound" else 1
        pm *= 0.55 if direction == "inbound" else 1
        weekday = (0.92, 1, 1.02, 1.08, 1.04)[local.weekday()]
        excess = weekday * (
            0.08
            + am * math.exp(-(((hour - 8.75) / 1.65) ** 2))
            + pm * math.exp(-(((hour - 18) / 2) ** 2))
            + 0.18 * math.exp(-(((hour - 13) / 3) ** 2))
        )
    return round(min(3.0, 1 + excess), 3)


def traverse(
    base_seconds: float,
    departure: datetime,
    corridor: str | None,
    direction: str = "both",
) -> float:
    """Integrate progress across 5-minute boundaries; preserves FIFO.

    Work is expressed in uncongested seconds, consumed at 1/k per real second.
    Applying just departure k would allow later cars to overtake earlier cars.
    """
    if not math.isfinite(base_seconds) or not 0 <= base_seconds <= 604800:
        raise ValueError("Invalid baseline duration")
    if departure.utcoffset() is None:
        raise ValueError("Departure must contain a UTC offset")
    remaining, elapsed = base_seconds, 0.0
    while remaining > 1e-8:
        current = departure + timedelta(seconds=elapsed)
        local = current.astimezone(MOSCOW)
        boundary = local.replace(
            minute=local.minute // INTERVAL_MINUTES * INTERVAL_MINUTES,
            second=0,
            microsecond=0,
        ) + timedelta(minutes=INTERVAL_MINUTES)
        available = (boundary - local).total_seconds()
        k = coefficient(corridor, current, direction) if corridor else 1.0
        consumed = min(remaining, available / k)
        elapsed += consumed * k
        remaining -= consumed
    return elapsed


def profile_table(day):
    midnight = datetime.combine(day, datetime.min.time(), MOSCOW)
    return {
        "model_version": VERSION,
        "quality": QUALITY,
        "timezone": "Europe/Moscow",
        "date": day.isoformat(),
        "interval_minutes": INTERVAL_MINUTES,
        "coverage_bounds": dict(
            zip(("south", "west", "north", "east"), SERVICE_BOUNDS, strict=True)
        ),
        "accuracy": {
            "status": "not_validated",
            "observed_trip_count": 0,
            "mae_minutes": None,
            "accuracy_percent": None,
        },
        "sources": SOURCES,
        "warning": (
            "Расчётный сценарий, не замеры 2026 года. "
            "Праздники, ДТП и погода не учтены."
        ),
        "rows": [
            {
                "corridor_id": key,
                "road": value[0],
                "direction": direction,
                "time": (
                    midnight + timedelta(minutes=slot * INTERVAL_MINUTES)
                ).strftime("%H:%M"),
                "coefficient": coefficient(
                    key,
                    midnight + timedelta(minutes=slot * INTERVAL_MINUTES),
                    direction,
                ),
            }
            for key, value in CORRIDORS.items()
            for direction in (
                ("both",) if key in UNDIRECTED else ("inbound", "outbound", "both")
            )
            for slot in range(SLOTS_PER_DAY)
        ],
    }
