from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from planning.domain.traffic import (
    MOSCOW,
    coefficient,
    corridor_for,
    profile_table,
    traverse,
)
from planning.infrastructure.adapters.traffic_route import (
    build_traffic_route,
    evaluate_trip,
)
from planning.presentation.http.project.traffic import TrafficRouteRequest


def shape(points):
    result, previous = "", [0, 0]
    for point in points:
        for i, coordinate in enumerate(point):
            current = round(coordinate * 1e6)
            delta = current - previous[i]
            previous[i] = current
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 32:
                result += chr((32 | (value & 31)) + 63)
                value >>= 5
            result += chr(value + 63)
    return result


def trip(seconds=600, road="МКАД", points=None):
    return {
        "legs": [
            {
                "shape": shape(points or [[55.8, 37.4], [55.81, 37.41]]),
                "summary": {"time": seconds},
                "maneuvers": [
                    {
                        "time": seconds,
                        "street_names": [road],
                        "begin_shape_index": 0,
                        "end_shape_index": 1,
                    }
                ],
            }
        ]
    }


def test_profiles_are_complete_and_explicitly_estimates():
    table = profile_table(date(2026, 9, 23))
    assert table["quality"] == "uncalibrated_estimate"
    rows = [r for r in table["rows"] if r["corridor_id"] == "urban"]
    assert len(rows) == 288
    assert len({r["time"] for r in rows}) == 288
    assert table["interval_minutes"] == 5
    assert rows[1]["time"] == "00:05"
    assert rows[-1]["time"] == "23:55"
    assert table["accuracy"]["accuracy_percent"] is None
    assert all(1 <= r["coefficient"] <= 3 for r in table["rows"])


def test_timezone_direction_and_weekend():
    at = datetime(2026, 9, 23, 8, 30, tzinfo=MOSCOW)
    assert coefficient("leningrad", at, "inbound") > coefficient(
        "leningrad", at, "outbound"
    )
    assert coefficient("ttk", at) == coefficient("ttk", at.astimezone(timezone.utc))
    assert coefficient("ttk", at) > coefficient("ttk", at + timedelta(days=3))
    with pytest.raises(ValueError):
        coefficient("ttk", at.replace(tzinfo=None))


def test_crossing_bucket_integrates_remaining_distance_and_fifo():
    at = datetime(2026, 9, 23, 8, 4, 50, tzinfo=MOSCOW)
    k1 = coefficient("ttk", at)
    k2 = coefficient("ttk", at + timedelta(seconds=10))
    assert traverse(100, at, "ttk") == pytest.approx(10 + (100 - 10 / k1) * k2)
    arrivals = [
        (
            at
            + timedelta(
                seconds=i * 10 + traverse(1800, at + timedelta(seconds=i * 10), "ttk")
            )
        )
        for i in range(150)
    ]
    assert arrivals == sorted(arrivals)
    assert traverse(100, at, None) == 100


def test_midnight_and_invalid_inputs():
    at = datetime(2026, 9, 25, 23, 59, tzinfo=MOSCOW)
    assert 3600 <= traverse(3600, at, "mkad") <= 10800
    for invalid in (-1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            traverse(invalid, at, "mkad")
    with pytest.raises(ValidationError):
        TrafficRouteRequest(departure_at="2026-09-23T08:00:00", stops=[])


def test_outside_region_gets_regional_profile_and_pedestrians_do_not():
    at = datetime(2026, 9, 23, 8, tzinfo=MOSCOW)
    outside = trip(points=[[56.8, 60.6], [56.81, 60.61]])
    assert corridor_for(["МКАД"], [[56.8, 60.6]]) is None
    assert evaluate_trip(outside, at, "auto")["uncovered_baseline_seconds"] == 0
    assert evaluate_trip(outside, at, "auto")["duration_seconds"] > 600
    assert evaluate_trip(trip(), at, "pedestrian")["duration_seconds"] == 600
    assert evaluate_trip(trip(), at, "auto")["duration_seconds"] > 600


@pytest.mark.asyncio
async def test_district_alternative_selection_and_service_shift_next_departure():
    request = TrafficRouteRequest(
        departure_at="2026-09-23T08:00:00+03:00",
        stops=[
            {"latitude": 55.8, "longitude": 37.4},
            {
                "latitude": 55.81,
                "longitude": 37.41,
                "job_id": 1,
                "service_seconds": 3600,
                "not_before": "2026-09-23T10:00:00+03:00",
            },
            {"latitude": 55.82, "longitude": 37.42, "job_id": 2},
        ],
    )
    client = AsyncMock()
    client.post.return_value = httpx.Response(
        200,
        request=httpx.Request("POST", "http://local/route"),
        json={"trip": trip(600, "ТТК"), "alternates": [{"trip": trip(650, "МСД")}]},
    )
    result = await build_traffic_route(request, SimpleNamespace(), client=client)
    assert result["legs"][0]["segments"][0]["corridor_id"] == "urban"
    assert result["legs"][0]["baseline_seconds"] == 600
    assert result["legs"][1]["departure_at"] == "2026-09-23T11:00:00+03:00"
    assert result["duration_seconds"] < 3600  # excludes service/waiting
    assert client.post.call_count == 2
    assert client.post.call_args.kwargs["json"]["costing_options"]["auto"][
        "speed_types"
    ] == ["freeflow"]


@pytest.mark.asyncio
async def test_backend_flag_disables_traffic_adjustment():
    request = TrafficRouteRequest(
        departure_at="2026-09-23T08:00:00+03:00",
        stops=[
            {"latitude": 55.8, "longitude": 37.4},
            {"latitude": 55.81, "longitude": 37.41},
        ],
    )
    client = AsyncMock()
    client.post.return_value = httpx.Response(
        200,
        request=httpx.Request("POST", "http://local/route"),
        json={"trip": trip(600)},
    )

    result = await build_traffic_route(
        request,
        SimpleNamespace(traffic_model_enabled=False),
        client=client,
    )

    assert result["traffic_enabled"] is False
    assert result["quality"] == "disabled"
    assert result["baseline_seconds"] == 600
    assert result["duration_seconds"] == 600
    assert result["delay_seconds"] == 0
    assert result["coefficient"] == 1
    assert all(
        option["duration_seconds"] == 600 for option in result["departure_options"]
    )
    assert result["legs"][0]["segments"][0]["quality"] == "disabled"


def test_planning_envelope_keeps_snapshot_baseline_and_unreachable_arcs():
    from planning.application.interfaces.travel_matrix_provider import TravelMatrix
    from planning.domain.entities.coordinate import Coordinate
    from planning.domain.enums import TransportType
    from planning.infrastructure.adapters.ortools_routing.travel import (
        _traffic_envelope,
    )

    data = SimpleNamespace(
        planning_date=date(2026, 9, 23),
        timezone="Europe/Moscow",
        engineers=[
            SimpleNamespace(
                transport_type=TransportType.CAR,
                shift_start_min=480,
                shift_end_min=1080,
            )
        ],
    )
    baseline = TravelMatrix(
        [[0, 600], [None, 0]], [[0, 1000], [None, 0]], "auto", "TEST"
    )
    points = [Coordinate(55.8, 37.4), Coordinate(55.81, 37.41)]
    result = _traffic_envelope(data, points, {"auto": baseline})["auto"]
    assert result.travel_time_seconds[0][1] > 600
    assert result.travel_time_seconds[1][0] is None
    assert baseline.travel_time_seconds[0][1] == 600
    assert result.distance_meters == baseline.distance_meters


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        ([55.43, 37.77], "domodedovo"),
        ([54.89, 38.07], "stupino"),
        ([54.84, 38.25], "kashira_local"),
        ([55.20, 37.8], "regional"),
        ([55.73, 37.7], "urban"),
    ],
)
def test_all_source_areas_have_profiles(point, expected):
    assert corridor_for(["Местная улица"], [point]) == expected


def test_highways_do_not_match_substrings_in_local_street_names():
    assert corridor_for(["М-4", "Дон"], [[54.95, 38.05], [55.20, 37.9]]) == "m4"
    assert corridor_for(["Задонский проезд"], [[55.61, 37.73]]) == "urban"
    assert corridor_for(["Садовая улица"], [[54.84, 38.25]]) == "kashira_local"


@pytest.mark.asyncio
async def test_access_wait_and_last_service_reconcile_whole_day():
    request = TrafficRouteRequest(
        departure_at="2026-09-23T08:00:00+03:00",
        stops=[
            {"latitude": 55.8, "longitude": 37.4},
            {
                "latitude": 55.81,
                "longitude": 37.41,
                "job_id": 1,
                "access_seconds": 300,
                "service_seconds": 1800,
                "not_before": "2026-09-23T09:00:00+03:00",
                "planned_start": "2026-09-23T09:00:00+03:00",
            },
        ],
    )
    client = AsyncMock()
    client.post.return_value = httpx.Response(
        200,
        request=httpx.Request("POST", "http://local/route"),
        json={"trip": trip(600)},
    )
    result = await build_traffic_route(request, SimpleNamespace(), client=client)
    assert result["finish_at"] == "2026-09-23T09:30:00+03:00"
    assert result["elapsed_seconds"] == 5400
    assert result["access_seconds"] == 300
    assert (
        abs(
            result["elapsed_seconds"]
            - sum(
                result[k]
                for k in (
                    "duration_seconds",
                    "waiting_seconds",
                    "access_seconds",
                    "service_seconds",
                )
            )
        )
        <= 1
    )
    assert result["late_stops"] == 0
    assert len(result["departure_options"]) == 13
    assert result["departure_options"][1]["departure_at"] == "2026-09-23T08:05:00+03:00"
    assert (
        result["departure_options"][0]["duration_seconds"] == result["duration_seconds"]
    )
    assert result["departure_options"][-1]["late_stops"] == 1
    assert client.post.call_count == 1  # forecasts reuse paths


@pytest.mark.asyncio
async def test_regional_shortcut_is_compared_by_its_district_time():
    request = TrafficRouteRequest(
        departure_at="2026-09-23T08:00:00+03:00",
        stops=[
            {"latitude": 55.8, "longitude": 37.4},
            {"latitude": 55.81, "longitude": 37.41},
        ],
    )
    client = AsyncMock()
    client.post.return_value = httpx.Response(
        200,
        request=httpx.Request("POST", "http://local/route"),
        json={
            "trip": trip(100, points=[[56.8, 60.6], [56.81, 60.61]]),
            "alternates": [{"trip": trip(600)}],
        },
    )
    result = await build_traffic_route(request, SimpleNamespace(), client=client)
    assert result["coverage_status"] == "complete"
    assert result["baseline_seconds"] == 100


def test_same_node_zero_length_route_is_valid():
    zero = {
        "legs": [
            {"shape": shape([[55.8, 37.4]]), "summary": {"time": 0}, "maneuvers": []}
        ]
    }
    result = evaluate_trip(zero, datetime(2026, 9, 23, 8, tzinfo=MOSCOW), "auto")
    assert result["duration_seconds"] == 0
