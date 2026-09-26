from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from planning.application.interfaces.travel_matrix_provider import TravelMatrix
from planning.application.validators.planning_result import PlanningValidator
from planning.domain.entities.coordinate import Coordinate
from planning.domain.enums import TransportType
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.ortools_routing.travel import (
    RoutingMatrices,
    prepare_travel_matrices,
    refine_transit_times_for_routes,
)
from planning.infrastructure.adapters.planning_solver_ortools import (
    OrToolsPlanningSolver,
)
from planning.infrastructure.adapters.traffic_route import build_traffic_route
from planning.infrastructure.adapters.travel_matrix_provider_valhalla import (
    ValhallaTravelMatrixProvider,
)
from planning.presentation.http.project.schemas import EngineerCreate, EngineerPatch
from planning.presentation.http.project.traffic import TrafficRouteRequest
from planning.presentation.http.project_workspace import build_project_traffic_route
from tests.unit.infrastructure.test_routing_regressions import (
    Provider,
    data,
    engineer,
    job,
)
from tests.unit.infrastructure.test_traffic import trip


@pytest.mark.parametrize("transport", list(TransportType))
async def test_transport_reaches_solver_validator_and_api(transport):
    planning = data([job(1)], [engineer(transport_type=transport)])
    provider = Provider()
    provider.get_matrix = AsyncMock(wraps=provider.get_matrix)
    result = await OrToolsPlanningSolver(provider).solve(planning)
    assert not result.unassigned
    assert PlanningValidator().validate(planning, result) == []
    assert set(result.travel_matrices) == {transport.routing_profile}
    assert provider.get_matrix.call_args.args[1] == transport.routing_profile
    assert (
        EngineerPatch(transport_type=transport.value).transport_type == transport.value
    )
    assert (
        EngineerCreate(
            name="Test", transport_type=transport.value, start_address="Test"
        ).transport_type
        == transport.value
    )


async def test_transit_snapshot_is_scoped_to_date_and_departure():
    planning = data([job(1)], [engineer(transport_type=TransportType.PUBLIC_TRANSPORT)])
    provider = Provider()
    provider.get_matrix_for_departure = AsyncMock(
        return_value=await provider.get_matrix([1, 2], "multimodal")
    )
    await prepare_travel_matrices(planning, provider)
    await prepare_travel_matrices(planning, provider)
    assert provider.get_matrix_for_departure.call_count == 1
    tomorrow = replace(
        planning, planning_date=planning.planning_date + timedelta(days=1)
    )
    await prepare_travel_matrices(tomorrow, provider)
    assert provider.get_matrix_for_departure.call_count == 2
    assert (
        provider.get_matrix_for_departure.call_args.args[2].date()
        == tomorrow.planning_date
    )


async def test_selected_transit_legs_are_repriced_at_their_own_departures():
    planning = data(
        [job(1), job(2)],
        [engineer(transport_type=TransportType.PUBLIC_TRANSPORT)],
    )
    start = datetime(2030, 1, 1, 8, tzinfo=timezone.utc)
    first_finish = start + timedelta(minutes=91)
    route = SimpleNamespace(
        engineer_id=1,
        planned_start=start,
        jobs=[
            SimpleNamespace(job_id=1, planned_finish=first_finish),
            SimpleNamespace(job_id=2, planned_finish=first_finish + timedelta(hours=1)),
        ],
    )
    base = [[0 if i == j else 60 for j in range(3)] for i in range(3)]
    travel = RoutingMatrices(
        {"multimodal": [row[:] for row in base]},
        {"multimodal": [row[:] for row in base]},
        {"multimodal": [row[:] for row in base]},
    )
    departures = []

    async def dated_pair(coordinates, profile, departure):
        departures.append(departure)
        duration = 1800 if departure == first_finish else 60
        return TravelMatrix(
            [[0, duration], [duration, 0]],
            [[0, 100], [100, 0]],
            profile,
            "TEST",
        )

    provider = SimpleNamespace(get_matrix_for_departure=dated_pair)
    refined = await refine_transit_times_for_routes(
        planning, travel, SimpleNamespace(routes=[route]), provider, {}
    )
    assert departures == [start, first_finish]
    assert refined.minutes["multimodal"][2][0] == 1
    assert refined.minutes["multimodal"][0][1] == 30
    assert refined.seconds["multimodal"][0][1] == 1800


async def test_solver_updates_later_transit_leg_before_publishing_schedule():
    class TimedProvider:
        async def get_matrix_for_departure(self, coordinates, profile, departure):
            size = len(coordinates)
            duration = 1800 if size == 2 and departure.hour >= 9 else 60
            return TravelMatrix(
                [[0 if i == j else duration for j in range(size)] for i in range(size)],
                [[0 if i == j else 100 for j in range(size)] for i in range(size)],
                profile,
                "TEST",
            )

    planning = data(
        [
            job(1, window_start_min=490, window_end_min=510, duration_min=90),
            job(2, window_start_min=600, window_end_min=660, duration_min=30),
        ],
        [engineer(transport_type=TransportType.PUBLIC_TRANSPORT)],
    )
    result = await OrToolsPlanningSolver(TimedProvider()).solve(planning)
    assert [item.job_id for item in result.routes[0].jobs] == [1, 2]
    assert result.routes[0].jobs[1].travel_from_previous_min == 30
    assert PlanningValidator().validate(planning, result) == []


async def test_project_scoped_traffic_route_does_not_use_legacy_dispatcher_scope():
    access = SimpleNamespace(project=AsyncMock(), dispatcher=AsyncMock())
    routes = SimpleNamespace(build=AsyncMock(return_value={"ok": True}))
    request = TrafficRouteRequest(
        departure_at="2026-09-26T08:00:00+03:00",
        profile="bicycle",
        stops=[
            {"latitude": 55.7, "longitude": 37.5},
            {"latitude": 55.8, "longitude": 37.6},
        ],
    )
    assert await build_project_traffic_route(7, request, access, routes) == {"ok": True}
    access.project.assert_awaited_once_with(7)
    access.dispatcher.assert_not_called()


async def test_mixed_team_uses_each_engineers_travel_times():
    minutes = {"auto": 2, "pedestrian": 20, "bicycle": 7, "multimodal": 12}

    class MixedProvider:
        async def get_matrix(self, coordinates, profile, cache_ttl_days=None):
            return TravelMatrix(
                [
                    [0 if a == b else minutes[profile] * 60 for b in coordinates]
                    for a in coordinates
                ],
                [[0 if a == b else 1000 for b in coordinates] for a in coordinates],
                profile,
                "TEST",
            )

    planning = data(
        [job(i, required_qualifications=frozenset({i})) for i in range(1, 5)],
        [
            engineer(i, transport_type=mode, qualifications=frozenset({i}))
            for i, mode in enumerate(TransportType, 1)
        ],
    )
    result = await OrToolsPlanningSolver(MixedProvider()).solve(planning)
    assert not result.unassigned
    assert PlanningValidator().validate(planning, result) == []
    for route in result.routes:
        profile = planning.engineers[
            route.engineer_id - 1
        ].transport_type.routing_profile
        assert route.total_travel_min == minutes[profile]


async def test_transit_provider_uses_dated_route_pairs_and_does_not_cache_as_roads():
    planning = data([job(1)], [engineer(transport_type=TransportType.PUBLIC_TRANSPORT)])
    session = AsyncMock()
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(
        200,
        request=httpx.Request("POST", "http://test/route"),
        json={"trip": {"summary": {"time": 123.2, "length": 1.5}}},
    )
    provider = ValhallaTravelMatrixProvider(
        session, PlanningServiceConfig("http://test", "http://test", "", 1, 40)
    )
    provider.get_matrix = AsyncMock(
        return_value=TravelMatrix(
            [[0, 600], [600, 0]],
            [[0, 800], [800, 0]],
            "pedestrian",
            "TEST_WALKING_BASELINE",
        )
    )
    with patch(
        "planning.infrastructure.adapters.travel_matrix_provider_valhalla.httpx.AsyncClient",
        return_value=client,
    ):
        result = await prepare_travel_matrices(planning, provider)
    assert result.seconds["multimodal"] == [[0, 124], [124, 0]]
    assert result.meters["multimodal"] == [[0, 1500], [1500, 0]]
    assert client.post.call_count == 2
    for call in client.post.call_args_list:
        assert call.args == ("/route",)
        assert call.kwargs["json"]["costing"] == "multimodal"
        assert call.kwargs["json"]["date_time"] == {
            "type": 1,
            "value": "2030-01-01T11:00",
        }
    session.execute.assert_not_called()


async def test_transit_provider_walks_when_gtfs_graph_is_unconnected():
    provider = ValhallaTravelMatrixProvider(
        AsyncMock(), PlanningServiceConfig("http://test", "http://test", "", 1, 40)
    )
    client = AsyncMock()
    client.__aenter__.return_value = client

    async def response_for_costing(_path, *, json):
        request = httpx.Request("POST", "http://test/route")
        if json["costing"] == "multimodal":
            return httpx.Response(400, request=request, json={"error_code": 170})
        return httpx.Response(
            200,
            request=request,
            json={"trip": {"summary": {"time": 600, "length": 1}}},
        )

    client.post.side_effect = response_for_costing
    with patch(
        "planning.infrastructure.adapters.travel_matrix_provider_valhalla.httpx.AsyncClient",
        return_value=client,
    ):
        result = await provider.get_matrix_for_departure(
            [Coordinate(55.7, 37.5), Coordinate(55.8, 37.6)],
            "multimodal",
            datetime(2026, 9, 26, 8, tzinfo=timezone.utc),
        )
    assert result.travel_time_seconds[0][1] == 600
    assert result.distance_meters[0][1] == 1000
    assert {call.kwargs["json"]["costing"] for call in client.post.call_args_list} == {
        "multimodal",
        "pedestrian",
    }


async def test_transit_provider_avoids_overnight_wait_when_walking_is_faster():
    provider = ValhallaTravelMatrixProvider(
        AsyncMock(), PlanningServiceConfig("http://test", "http://test", "", 1, 40)
    )
    client = AsyncMock()
    client.__aenter__.return_value = client

    async def response_for_costing(_path, *, json):
        seconds = 23740 if json["costing"] == "multimodal" else 10909
        return httpx.Response(
            200,
            request=httpx.Request("POST", "http://test/route"),
            json={"trip": {"summary": {"time": seconds, "length": 17}}},
        )

    client.post.side_effect = response_for_costing
    with patch(
        "planning.infrastructure.adapters.travel_matrix_provider_valhalla.httpx.AsyncClient",
        return_value=client,
    ):
        result = await provider.get_matrix_for_departure(
            [Coordinate(55.7, 37.5), Coordinate(55.8, 37.6)],
            "multimodal",
            datetime(2026, 9, 27, 20, 30, tzinfo=timezone.utc),
        )
    assert result.travel_time_seconds[0][1] == 10909
    assert result.travel_time_seconds[1][0] == 10909
    assert {call.kwargs["json"]["costing"] for call in client.post.call_args_list} == {
        "multimodal",
        "pedestrian",
    }


async def test_transit_matrix_keeps_multimodal_values_when_available():
    config = PlanningServiceConfig("http://test", "http://test", "", 1, 40)
    provider = ValhallaTravelMatrixProvider(AsyncMock(), config)
    provider.get_matrix = AsyncMock(
        return_value=TravelMatrix(
            [[0, 100, 600], [100, 0, 500], [600, 500, 0]],
            [[0, 100, 600], [100, 0, 500], [600, 500, 0]],
            "pedestrian",
            "TEST",
        )
    )
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(
        200,
        request=httpx.Request("POST", "http://test/route"),
        json={"trip": {"summary": {"time": 120, "length": 0.5}}},
    )
    with patch(
        "planning.infrastructure.adapters.travel_matrix_provider_valhalla.httpx.AsyncClient",
        return_value=client,
    ):
        matrix = await provider.get_matrix_for_departure(
            [Coordinate(55.7, 37.5), Coordinate(55.8, 37.6), Coordinate(55.9, 37.7)],
            "multimodal",
            datetime(2026, 9, 25, 8, tzinfo=timezone.utc),
        )
    assert client.post.call_count == 6
    assert matrix.travel_time_seconds[0][2] == 120
    assert matrix.travel_time_seconds[0][1] == 120
    assert matrix.source == "VALHALLA_TRANSIT"


@pytest.mark.parametrize("profile", ["pedestrian", "bicycle", "multimodal"])
async def test_map_uses_transport_profile_without_car_traffic(profile):
    request = TrafficRouteRequest(
        departure_at="2026-09-23T08:00:00+03:00",
        profile=profile,
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
    result = await build_traffic_route(request, SimpleNamespace(), client=client)
    payload = client.post.call_args.kwargs["json"]
    assert payload["costing"] == profile
    assert result["duration_seconds"] == 600
    assert result["delay_seconds"] == 0
    if profile == "multimodal":
        assert payload["date_time"]["type"] == 1
        assert "departure_options" not in result


async def test_multimodal_map_falls_back_to_walk_when_gtfs_is_missing():
    request = TrafficRouteRequest(
        departure_at="2026-09-26T08:00:00+03:00",
        profile="multimodal",
        stops=[
            {"latitude": 55.8, "longitude": 37.4},
            {"latitude": 55.81, "longitude": 37.41},
        ],
    )
    client = AsyncMock()

    async def response_for_costing(_path, *, json):
        http_request = httpx.Request("POST", "http://local/route")
        if json["costing"] == "multimodal":
            return httpx.Response(400, request=http_request, json={"error_code": 170})
        return httpx.Response(200, request=http_request, json={"trip": trip(600)})

    client.post.side_effect = response_for_costing
    result = await build_traffic_route(request, SimpleNamespace(), client=client)
    assert result["duration_seconds"] == 600
    assert result["legs"][0]["provider"] == "PEDESTRIAN_FALLBACK"
    assert "использован пеший маршрут" in result["warning"]
    assert any("использован пеший путь" in value for value in result["warnings"])


async def test_multimodal_map_prefers_walk_to_overnight_wait():
    request = TrafficRouteRequest(
        departure_at="2026-09-27T23:30:00+03:00",
        profile="multimodal",
        stops=[
            {"latitude": 55.8, "longitude": 37.4},
            {"latitude": 55.81, "longitude": 37.41},
        ],
    )
    client = AsyncMock()

    async def response_for_costing(_path, *, json):
        seconds = 23740 if json["costing"] == "multimodal" else 10909
        return httpx.Response(
            200,
            request=httpx.Request("POST", "http://local/route"),
            json={"trip": trip(seconds)},
        )

    client.post.side_effect = response_for_costing
    result = await build_traffic_route(request, SimpleNamespace(), client=client)
    assert result["duration_seconds"] == 10909
    assert result["legs"][0]["provider"] == "PEDESTRIAN_FASTER"
    assert "Пеший маршрут быстрее" in result["warning"]
    assert any("пеший путь быстрее" in value for value in result["warnings"])
