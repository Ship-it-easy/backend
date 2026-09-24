"""Получение дорожных данных, фиксация snapshot и перевод секунд в минуты."""

import asyncio
import math
import time as monotonic_time
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from planning.application.errors import SolverTimeLimit
from planning.application.interfaces.travel_matrix_provider import (
    TravelMatrix,
    TravelMatrixProvider,
)
from planning.domain.entities.coordinate import Coordinate
from planning.domain.entities.planning import PlanningInput
from planning.domain.enums import TransportType
from planning.domain.traffic import CORRIDORS, INTERVAL_MINUTES, coefficient, covered

MatrixByProfile = dict[str, list[list[int | None]]]


@dataclass(frozen=True)
class RoutingMatrices:
    """Одинаковый порядок строк: заявки, затем старты инженеров.

    minutes — для расписания; seconds/meters — для стоимости и аудита.
    None означает отсутствие дороги; подменять его нулём нельзя.
    """

    minutes: MatrixByProfile
    seconds: MatrixByProfile
    meters: MatrixByProfile


async def prepare_travel_matrices(
    data: PlanningInput, matrix_provider: TravelMatrixProvider
) -> RoutingMatrices:
    """Загрузить матрицы в рамках общего дедлайна события."""
    coordinates = [job.coordinate for job in data.jobs] + [
        engineer.coordinate for engineer in data.engineers
    ]
    profiles = {
        "auto" if engineer.transport_type == TransportType.CAR else "pedestrian"
        for engineer in data.engineers
    }

    remaining = (
        data.solve_deadline_monotonic - monotonic_time.monotonic()
        if data.solve_deadline_monotonic is not None
        else None
    )
    try:
        async with asyncio.timeout(remaining):
            raw_matrices = await _load_snapshot_matrices(
                data, coordinates, profiles, matrix_provider
            )
    except TimeoutError as error:
        raise SolverTimeLimit("MATRIX_TIME_LIMIT") from error
    if getattr(matrix_provider, "planning_traffic_enabled", False):
        raw_matrices = _traffic_envelope(data, coordinates, raw_matrices)
    return _convert_matrices(raw_matrices, len(coordinates))


def _traffic_envelope(data, coordinates, raw_matrices):
    """Conservative shift envelope for the static VRP, not departure-specific ETA.

    Baselines alone stay in the event snapshot/cache. Recalculate this bound for
    each planning date so future candidates cannot reuse today's traffic factor.
    """
    drivers = [e for e in data.engineers if e.transport_type == TransportType.CAR]
    if not drivers or "auto" not in raw_matrices:
        return raw_matrices
    midnight = datetime.combine(data.planning_date, time.min, ZoneInfo(data.timezone))
    start = (
        min(e.shift_start_min for e in drivers) // INTERVAL_MINUTES * INTERVAL_MINUTES
    )
    end = max(e.shift_end_min for e in drivers)
    factor = max(
        coefficient(key, midnight + timedelta(minutes=minute))
        for key in CORRIDORS
        for minute in range(
            start, max(start, end) + INTERVAL_MINUTES + 1, INTERVAL_MINUTES
        )
    )
    raw = raw_matrices["auto"]
    within = [covered(c.latitude, c.longitude) for c in coordinates]
    # No claim of congestion coverage outside the model's geographic boundary.
    adjusted = [
        [
            math.ceil(value * factor)
            if value is not None and within[i] and within[j]
            else value
            for j, value in enumerate(row)
        ]
        for i, row in enumerate(raw.travel_time_seconds)
    ]
    return {
        **raw_matrices,
        "auto": TravelMatrix(
            adjusted, raw.distance_meters, "auto", "MOSCOW_SCENARIO_SHIFT_ENVELOPE"
        ),
    }


async def _load_snapshot_matrices(
    data: PlanningInput,
    coordinates: list[Coordinate],
    profiles: set[str],
    matrix_provider: TravelMatrixProvider,
) -> dict[str, TravelMatrix]:
    result = {}
    for profile in sorted(profiles):
        keys = [
            [
                (profile, a.latitude, a.longitude, b.latitude, b.longitude)
                for b in coordinates
            ]
            for a in coordinates
        ]
        if any(key not in data.travel_snapshot for row in keys for key in row):
            try:
                matrix = await matrix_provider.get_matrix(
                    coordinates,
                    profile,
                    data.config.travel_cache_ttl_days,
                )
            except Exception as error:
                raise RuntimeError("TRAVEL_PROVIDER_UNAVAILABLE") from error
            size = len(coordinates)
            if any(
                len(values) != size or any(len(row) != size for row in values)
                for values in (
                    matrix.travel_time_seconds,
                    matrix.distance_meters,
                )
            ):
                raise RuntimeError("INVALID_TRAVEL_MATRIX_SHAPE")
            for i, row in enumerate(keys):
                for j, key in enumerate(row):
                    # Все кандидаты события используют первое полученное значение
                    # пары, даже если следующий запрос содержит другие точки.
                    data.travel_snapshot.setdefault(
                        key,
                        (
                            matrix.travel_time_seconds[i][j],
                            matrix.distance_meters[i][j],
                        ),
                    )
        result[profile] = TravelMatrix(
            [[data.travel_snapshot[key][0] for key in row] for row in keys],
            [[data.travel_snapshot[key][1] for key in row] for row in keys],
            profile,
            "EVENT_SNAPSHOT",
        )
    return result


def _convert_matrices(
    raw_matrices: dict[str, TravelMatrix], expected_size: int
) -> RoutingMatrices:
    """Проверить размеры и значения; округлить каждый переезд вверх до минуты."""
    matrices: dict[str, list[list[int | None]]] = {}
    seconds_matrices: dict[str, list[list[int | None]]] = {}
    distance_matrices: dict[str, list[list[int | None]]] = {}
    for profile, raw in raw_matrices.items():
        if isinstance(raw, TravelMatrix):
            if (
                len(raw.travel_time_seconds) != expected_size
                or len(raw.distance_meters) != expected_size
                or any(
                    len(time_row) != expected_size
                    or len(distance_row) != expected_size
                    or len(time_row) != len(distance_row)
                    for time_row, distance_row in zip(
                        raw.travel_time_seconds,
                        raw.distance_meters,
                        strict=True,
                    )
                )
            ):
                raise RuntimeError("DISTANCE_DATA_NOT_READY")
            matrices[profile] = [
                [None if value is None else math.ceil(value / 60) for value in row]
                for row in raw.travel_time_seconds
            ]
            seconds_matrices[profile] = raw.travel_time_seconds
            distance_matrices[profile] = raw.distance_meters
            for time_row, distance_row in zip(
                raw.travel_time_seconds, raw.distance_meters, strict=True
            ):
                if any(
                    (value is not None and (not isinstance(value, int) or value < 0))
                    for value in (*time_row, *distance_row)
                ):
                    raise RuntimeError("DISTANCE_DATA_NOT_READY")
                if any(
                    (time_value is None) != (distance_value is None)
                    for time_value, distance_value in zip(
                        time_row, distance_row, strict=True
                    )
                ):
                    raise RuntimeError("DISTANCE_DATA_NOT_READY")
        else:
            raise RuntimeError("DISTANCE_DATA_NOT_READY")
    return RoutingMatrices(matrices, seconds_matrices, distance_matrices)
