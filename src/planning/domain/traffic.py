"""Deterministic offline traffic adjustments for Moscow service routes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


HOURLY_PROFILES: dict[str, tuple[tuple[int, float], ...]] = {
    "yuvao_volgogradka_ryazanka": (
        (0, 1.0), (5, 1.05), (6, 1.2), (7, 1.5), (8, 1.6), (9, 1.4),
        (10, 1.3), (11, 1.35), (12, 1.4), (13, 1.4), (14, 1.35),
        (15, 1.45), (16, 1.65), (17, 1.8), (18, 1.85), (19, 1.75),
        (20, 1.5), (21, 1.25), (22, 1.1), (23, 1.0),
    ),
    "south_kashirka_biryulevo": (
        (0, 1.0), (5, 1.05), (6, 1.2), (7, 1.55), (8, 1.65), (9, 1.45),
        (10, 1.35), (11, 1.4), (12, 1.45), (13, 1.45), (14, 1.4),
        (15, 1.5), (16, 1.7), (17, 1.85), (18, 1.9), (19, 1.8),
        (20, 1.55), (21, 1.3), (22, 1.1), (23, 1.0),
    ),
    "yugocentr_profsoyuznaya_varshavka": (
        (0, 1.0), (5, 1.05), (6, 1.2), (7, 1.55), (8, 1.7), (9, 1.5),
        (10, 1.4), (11, 1.45), (12, 1.5), (13, 1.5), (14, 1.45),
        (15, 1.55), (16, 1.8), (17, 1.95), (18, 1.9), (19, 1.75),
        (20, 1.5), (21, 1.25), (22, 1.1), (23, 1.0),
    ),
    "center_ttk_sadovoe": (
        (0, 1.0), (5, 1.05), (6, 1.25), (7, 1.65), (8, 1.85), (9, 1.75),
        (10, 1.55), (11, 1.55), (12, 1.65), (13, 1.65), (14, 1.6),
        (15, 1.75), (16, 2.0), (17, 2.15), (18, 2.2), (19, 2.1),
        (20, 1.75), (21, 1.4), (22, 1.15), (23, 1.0),
    ),
    "oblast_domodedovo_kashira_stupino": (
        (0, 1.0), (5, 1.05), (6, 1.2), (7, 1.5), (8, 1.6), (9, 1.4),
        (10, 1.25), (11, 1.25), (12, 1.3), (13, 1.3), (14, 1.35),
        (15, 1.4), (16, 1.55), (17, 1.7), (18, 1.8), (19, 1.65),
        (20, 1.35), (21, 1.2), (22, 1.1), (23, 1.0),
    ),
    "moscow_default": (
        (0, 1.0), (5, 1.05), (6, 1.2), (7, 1.5), (8, 1.6), (9, 1.4),
        (10, 1.35), (11, 1.4), (12, 1.45), (13, 1.45), (14, 1.4),
        (15, 1.5), (16, 1.7), (17, 1.85), (18, 1.9), (19, 1.8),
        (20, 1.55), (21, 1.3), (22, 1.1), (23, 1.0),
    ),
}


@dataclass(frozen=True)
class TrafficModel:
    """Pure model: local baseline multipliers, tunable reliability and season."""

    enabled: bool = True
    reliability_buffer: float = 1.15

    def multiplier(self, profile: str, departure: date, minute: int) -> float:
        if not self.enabled:
            return 1.0
        profile_points = HOURLY_PROFILES.get(profile, HOURLY_PROFILES["moscow_default"])
        minute = max(0, min(1439, int(minute)))
        hour = minute // 60
        fraction = (minute % 60) / 60
        left = profile_points[hour][1]
        right = profile_points[(hour + 1) % 24][1]
        hourly = left + (right - left) * fraction

        if departure.weekday() == 4 and minute >= 16 * 60:
            day = 1.10 if profile.startswith("oblast_") else 1.05
        elif departure.weekday() == 5:
            day = 0.75
        elif departure.weekday() == 6:
            day = 1.10 if profile.startswith("oblast_") and 16 * 60 <= minute < 21 * 60 else 0.80
        else:
            day = 1.0

        season = 1.05 if departure.month == 9 else 1.10 if departure.month == 12 and minute >= 16 * 60 else 0.97 if departure.month == 8 else 1.0
        return round(hourly * day * season * self.reliability_buffer, 6)

