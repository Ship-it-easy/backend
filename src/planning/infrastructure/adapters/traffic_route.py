"""Evaluate Valhalla candidate paths at each engineer's actual departure time."""

import math
from datetime import timedelta

import httpx

from planning.domain.traffic import (
    INTERVAL_MINUTES,
    MOSCOW,
    QUALITY,
    SOURCES,
    VERSION,
    direction_for,
    district_profile_for,
    traverse,
)
from planning.infrastructure.adapters.valhalla_response import route_unavailable


def decode_shape(encoded: str) -> list[list[float]]:
    coordinates, values = [], []
    value = shift = 0
    for char in encoded:
        byte = ord(char) - 63
        if not 0 <= byte <= 63 or shift > 30:
            raise ValueError("Invalid Valhalla shape")
        value |= (byte & 31) << shift
        if byte >= 32:
            shift += 5
            continue
        values.append(~(value >> 1) if value & 1 else value >> 1)
        value = shift = 0
    if shift or len(values) % 2:
        raise ValueError("Incomplete Valhalla shape")
    lat = lon = 0
    for i in range(0, len(values), 2):
        lat += values[i]
        lon += values[i + 1]
        coordinates.append([lat / 1e6, lon / 1e6])
    return coordinates


def evaluate_trip(trip, departure, profile, traffic_enabled=True):
    elapsed = baseline = 0.0
    segments, points = [], []
    for leg in trip["legs"]:
        shape = decode_shape(leg["shape"])
        summary = float(leg["summary"]["time"])
        if not shape or (len(shape) < 2 and summary > 0):
            raise ValueError("Route geometry is missing")
        points.extend(shape if not points else shape[1:])
        if summary == 0:
            continue
        maneuvers = leg.get("maneuvers", [])
        total = sum(float(m["time"]) for m in maneuvers)
        if not math.isfinite(summary) or summary < 0 or total <= 0:
            raise ValueError("Route timings are missing")
        # Preserve total baseline time, including provider rounding/turn costs.
        for maneuver in maneuvers:
            seconds = float(maneuver["time"]) * summary / total
            if seconds == 0:
                continue
            begin, end = maneuver["begin_shape_index"], maneuver["end_shape_index"]
            if not 0 <= begin <= end < len(shape):
                raise ValueError("Invalid maneuver geometry")
            part = shape[begin : end + 1]
            names = maneuver.get("street_names", [])
            travel_mode = maneuver.get("travel_mode")
            corridor = district_profile_for(part) if profile == "auto" else None
            direction = direction_for(part, corridor)
            entered = departure + timedelta(seconds=elapsed)
            duration = (
                traverse(seconds, entered, corridor, direction)
                if traffic_enabled
                else seconds
            )
            segments.append(
                {
                    "road": ", ".join(names) or "Без названия",
                    "travel_mode": travel_mode,
                    "travel_type": maneuver.get("travel_type"),
                    "corridor_id": corridor,
                    "traffic_source": "district" if corridor else "not_applicable",
                    "direction": direction,
                    "points": part,
                    "departure_at": entered.isoformat(),
                    "baseline_seconds": seconds,
                    "duration_seconds": duration,
                    "coefficient": (
                        round(duration / seconds, 3)
                        if corridor and traffic_enabled
                        else 1
                    ),
                    "quality": (
                        "district_estimate"
                        if corridor and traffic_enabled
                        else (
                            "disabled"
                            if profile == "auto" and not traffic_enabled
                            else (
                                "not_applicable"
                                if profile != "auto"
                                else "no_traffic_data"
                            )
                        )
                    ),
                }
            )
            elapsed += duration
            baseline += seconds
    return {
        "points": points,
        "segments": segments,
        "duration_seconds": elapsed,
        "baseline_seconds": baseline,
        "uncovered_baseline_seconds": 0,
        "district_fallback_baseline_seconds": baseline if profile == "auto" else 0,
        "coefficient": round(elapsed / baseline, 3) if baseline else 1,
    }


def stop_timing(arrival, stop):
    """Parking/access is explicit. Appointment windows are not service duration."""
    ready = arrival + timedelta(seconds=stop.access_seconds)
    start = max(ready, stop.not_before) if stop.not_before else ready
    finish = start + timedelta(seconds=stop.service_seconds)
    lateness = (
        max(0, (start - stop.planned_start).total_seconds())
        if stop.planned_start
        else 0
    )
    return {
        "ready_at": ready.isoformat(),
        "service_start_at": start.isoformat(),
        "service_finish_at": finish.isoformat(),
        "waiting_seconds": (start - ready).total_seconds(),
        "access_seconds": stop.access_seconds,
        "service_seconds": stop.service_seconds,
        "planned_start": stop.planned_start.isoformat() if stop.planned_start else None,
        "late_to_plan_seconds": math.ceil(lateness),
    }, finish


def departure_options(request, legs, traffic_enabled=True):
    """Re-evaluate fixed selected paths, no additional geoservice requests.

    These are what-if scenarios on the same paths; time-dependent access
    restrictions and alternate route choices are not recomputed.
    """
    options = []
    for minutes in range(0, 61, INTERVAL_MINUTES):
        departure = request.departure_at + timedelta(minutes=minutes)
        _, current = stop_timing(departure, request.stops[0])
        driving = 0.0
        late_stops = 0
        arrival = current
        for leg, destination in zip(legs, request.stops[1:], strict=True):
            for segment in leg["segments"]:
                duration = (
                    traverse(
                        segment["baseline_seconds"],
                        current,
                        segment["corridor_id"],
                        segment["direction"],
                    )
                    if traffic_enabled
                    else segment["baseline_seconds"]
                )
                current += timedelta(seconds=duration)
                driving += duration
            arrival = current
            timing, current = stop_timing(arrival, destination)
            late_stops += timing["late_to_plan_seconds"] > 0
        options.append(
            {
                "departure_at": departure.isoformat(),
                "duration_seconds": math.ceil(driving),
                "arrival_at": arrival.isoformat(),
                "finish_at": current.isoformat(),
                "late_stops": late_stops,
            }
        )
    return options


async def build_traffic_route(request, config, *, client=None):
    traffic_enabled = getattr(config, "traffic_model_enabled", True)
    if client is None:
        async with httpx.AsyncClient(
            base_url=config.valhalla_url, timeout=config.geoservice_timeout_sec
        ) as owned:
            return await build_traffic_route(request, config, client=owned)
    start_timing, current = stop_timing(request.departure_at, request.stops[0])
    legs = []
    for origin, destination in zip(request.stops, request.stops[1:], strict=False):
        if (origin.latitude, origin.longitude) == (
            destination.latitude,
            destination.longitude,
        ):
            chosen = {
                "points": [[origin.latitude, origin.longitude]],
                "segments": [],
                "duration_seconds": 0,
                "baseline_seconds": 0,
                "uncovered_baseline_seconds": 0,
                "district_fallback_baseline_seconds": 0,
                "coefficient": 1,
            }
        else:
            response = await client.post(
                "/route",
                json={
                    "locations": [
                        {"lat": s.latitude, "lon": s.longitude}
                        for s in (origin, destination)
                    ],
                    "costing": request.profile,
                    "units": "kilometers",
                    "shape_format": "polyline6",
                    "alternates": 2,
                    # Disable predicted/current speeds to avoid double counting.
                    # With no freeflow feed Valhalla falls back to its OSM base speed.
                    "costing_options": (
                        {"auto": {"speed_types": ["freeflow"]}}
                        if request.profile == "auto"
                        else {}
                    ),
                    "date_time": {
                        # Type 1 suppresses alternatives in Valhalla.
                        "type": 3,
                        "value": current.astimezone(MOSCOW).strftime("%Y-%m-%dT%H:%M"),
                    },
                    "language": "ru-RU",
                },
            )
            valhalla_unreachable = route_unavailable(response)
            evaluated = []
            if not valhalla_unreachable:
                response.raise_for_status()
                payload = response.json()
                candidates = [payload["trip"]] + [
                    a["trip"] for a in payload.get("alternates", [])
                ]
                evaluated = [
                    evaluate_trip(trip, current, request.profile, traffic_enabled)
                    for trip in candidates
                ]
            if not evaluated:
                response.raise_for_status()
                raise ValueError("No route found")
            # Every auto segment has a district profile, so candidates are
            # comparable by their time-adjusted duration in either mode.
            chosen = min(
                evaluated,
                key=lambda item: item["duration_seconds"],
            )
            chosen["alternatives_evaluated"] = len(evaluated)
        chosen["departure_at"] = current.isoformat()
        current += timedelta(seconds=chosen["duration_seconds"])
        chosen["arrival_at"] = current.isoformat()
        chosen["job_id"] = destination.job_id
        timing, current = stop_timing(current, destination)
        chosen.update(timing)
        legs.append(chosen)
    baseline = sum(leg["baseline_seconds"] for leg in legs)
    seconds = sum(leg["duration_seconds"] for leg in legs)
    district_fallback = sum(leg["district_fallback_baseline_seconds"] for leg in legs)
    result = {
        "model_version": VERSION,
        "traffic_enabled": traffic_enabled,
        "quality": QUALITY if traffic_enabled else "disabled",
        "sources": SOURCES,
        "departure_at": request.departure_at.isoformat(),
        "arrival_at": legs[-1]["arrival_at"],
        "finish_at": current.isoformat(),
        "elapsed_seconds": math.ceil((current - request.departure_at).total_seconds()),
        "service_seconds": sum(stop.service_seconds for stop in request.stops),
        "access_seconds": sum(stop.access_seconds for stop in request.stops),
        "waiting_seconds": math.ceil(
            start_timing["waiting_seconds"]
            + sum(leg["waiting_seconds"] for leg in legs)
        ),
        "late_stops": sum(leg["late_to_plan_seconds"] > 0 for leg in legs),
        "interval_minutes": INTERVAL_MINUTES,
        "accuracy": {
            "status": "not_validated",
            "observed_trip_count": 0,
            "accuracy_percent": None,
            "mae_minutes": None,
        },
        "duration_seconds": math.ceil(seconds),
        "baseline_seconds": math.ceil(baseline),
        "delay_seconds": math.ceil(seconds) - math.ceil(baseline),
        "coefficient": round(seconds / baseline, 3) if baseline else 1,
        "coverage_fraction": 1 if request.profile == "auto" else None,
        "coverage_status": "complete"
        if request.profile == "auto"
        else "not_applicable",
        "district_fallback_fraction": (
            round(district_fallback / baseline, 3) if baseline else 0
        )
        if request.profile == "auto"
        else None,
        "warnings": (
            ["Для всех автомобильных дорог применён районный коэффициент по времени."]
            if district_fallback and traffic_enabled
            else []
        )
        + (
            ["Парковка и проход к клиенту не включены: время доступа не задано."]
            if request.profile == "auto"
            and not any(s.access_seconds for s in request.stops[1:])
            else []
        ),
        "legs": legs,
        "warning": (
            "Оценка по сценарию, не текущие пробки. "
            "Выбор среди вариантов Valhalla; порядок заявок сохранён."
            if traffic_enabled
            else "Модель пробок выключена: используется базовое время Valhalla."
        ),
    }
    if request.profile != "auto":
        result["warning"] = "Время маршрута без автомобильных коэффициентов пробок."
    if request.include_departure_options:
        result["departure_options"] = departure_options(request, legs, traffic_enabled)
        result["departure_options_basis"] = "same_paths_scenario_not_reoptimized"
    return result
