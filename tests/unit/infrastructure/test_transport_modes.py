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
    prepare_travel_matrices,
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


async def test_large_transit_matrix_has_bounded_route_requests():
    config = PlanningServiceConfig("http://test", "http://test", "", 1, 40)
    config.transit_matrix_route_limit = 2
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
    assert client.post.call_count == 2
    assert matrix.travel_time_seconds[0][2] == 120
    assert matrix.travel_time_seconds[0][1] == 100
    assert matrix.source.endswith("WALKING_FALLBACK")


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
