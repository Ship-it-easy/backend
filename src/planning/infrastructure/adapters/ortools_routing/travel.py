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
from planning.domain.traffic import (
    INTERVAL_MINUTES,
    coefficient,
    district_profile_for,
    traverse,
)

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
        TransportType(engineer.transport_type).routing_profile
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
    # Validate provider output before traffic coefficients can round invalid
    # fractional values into apparently valid integer travel times.
    _convert_matrices(raw_matrices, len(coordinates))
    if getattr(matrix_provider, "planning_traffic_enabled", False):
        raw_matrices = _traffic_envelope(data, coordinates, raw_matrices)
    return _convert_matrices(raw_matrices, len(coordinates))


def _traffic_envelope(data, coordinates, raw_matrices):
    """District-aware static approximation for the VRP, not departure-specific ETA.

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
    # OR-Tools consumes a static matrix.  Use the middle of the actual shift
    # and the district between the pair, rather than applying the worst peak of
    # every Moscow corridor to every road in the plan.
    representative = midnight + timedelta(
        minutes=(start + max(start, end)) // 2 // INTERVAL_MINUTES * INTERVAL_MINUTES
    )
    raw = raw_matrices["auto"]
    adjusted = [
        [
            math.ceil(
                value
                * coefficient(
                    district_profile_for(
                        [
                            [coordinates[i].latitude, coordinates[i].longitude],
                            [coordinates[j].latitude, coordinates[j].longitude],
                        ]
                    ),
                    representative,
                )
            )
            if value is not None
            else value
            for j, value in enumerate(row)
        ]
        for i, row in enumerate(raw.travel_time_seconds)
    ]
    return {
        **raw_matrices,
        "auto": TravelMatrix(
            adjusted,
            raw.distance_meters,
            "auto",
            "DISTRICT_SCENARIO_INITIAL_PASS",
        ),
    }


def refine_district_times_for_routes(data, travel, result):
    """Reprice the selected automobile arcs at their actual five-minute times.

    OR-Tools requires a static matrix.  The first pass gives an order; this
    pass writes the time-dependent cost of that order back into a copy of the
    matrix for one corrective solve.
    """
    if "auto" not in travel.minutes:
        return travel
    minutes = {
        profile: [row[:] for row in values]
        for profile, values in travel.minutes.items()
    }
    seconds = {
        profile: [row[:] for row in values]
        for profile, values in travel.seconds.items()
    }
    meters = {
        profile: [row[:] for row in values] for profile, values in travel.meters.items()
    }
    job_index = {job.id: index for index, job in enumerate(data.jobs)}
    engineer_index = {
        engineer.id: index for index, engineer in enumerate(data.engineers)
    }
    coordinates = [job.coordinate for job in data.jobs] + [
        engineer.coordinate for engineer in data.engineers
    ]

    for route in result.routes:
        engineer = data.engineers[engineer_index[route.engineer_id]]
        if engineer.transport_type != TransportType.CAR:
            continue
        previous = len(data.jobs) + engineer_index[route.engineer_id]
        departure = route.planned_start
        for route_job in route.jobs:
            destination = job_index[route_job.job_id]
            origin_coordinate = coordinates[previous]
            destination_coordinate = coordinates[destination]
            key = (
                "auto",
                origin_coordinate.latitude,
                origin_coordinate.longitude,
                destination_coordinate.latitude,
                destination_coordinate.longitude,
            )
            baseline = data.travel_snapshot.get(key, (None, None))[0]
            if baseline is not None:
                district = district_profile_for(
                    [
                        [origin_coordinate.latitude, origin_coordinate.longitude],
                        [
                            destination_coordinate.latitude,
                            destination_coordinate.longitude,
                        ],
                    ]
                )
                elapsed = math.ceil(traverse(baseline, departure, district))
                seconds["auto"][previous][destination] = elapsed
                minutes["auto"][previous][destination] = math.ceil(elapsed / 60)
            departure = route_job.planned_finish
            previous = destination
    return RoutingMatrices(minutes, seconds, meters)


async def _load_snapshot_matrices(
    data: PlanningInput,
    coordinates: list[Coordinate],
    profiles: set[str],
    matrix_provider: TravelMatrixProvider,
) -> dict[str, TravelMatrix]:
    result = {}
    for profile in sorted(profiles):
        departure = datetime.combine(
            data.planning_date, time.min, ZoneInfo(data.timezone)
        ) + timedelta(
            minutes=min(
                e.shift_start_min
                for e in data.engineers
                if TransportType(e.transport_type).routing_profile == profile
            )
        )
        # Transit schedules must not leak between planning days in a batch.
        snapshot_profile = (
            f"{profile}:{departure.isoformat()}" if profile == "multimodal" else profile
        )
        keys = [
            [
                (snapshot_profile, a.latitude, a.longitude, b.latitude, b.longitude)
                for b in coordinates
            ]
            for a in coordinates
        ]
        if any(key not in data.travel_snapshot for row in keys for key in row):
            try:
                dated_provider = getattr(
                    matrix_provider, "get_matrix_for_departure", None
                )
                if profile == "multimodal" and dated_provider is not None:
                    matrix = await dated_provider(coordinates, profile, departure)
                else:
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
