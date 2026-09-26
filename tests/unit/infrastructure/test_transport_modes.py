from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from planning.application.validators.planning_result import PlanningValidator
from planning.domain.enums import TransportType
from planning.infrastructure.adapters.planning_solver_ortools import (
    OrToolsPlanningSolver,
)
from planning.infrastructure.adapters.traffic_route import build_traffic_route
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


def test_retired_transport_is_pedestrian_only_for_stored_values():
    assert TransportType("PUBLIC_TRANSPORT") is TransportType.NONE
    assert "PUBLIC_TRANSPORT" not in [transport.value for transport in TransportType]
    with pytest.raises(ValidationError):
        EngineerPatch(transport_type="PUBLIC_TRANSPORT")
    with pytest.raises(ValidationError):
        TrafficRouteRequest(
            departure_at="2026-09-26T08:00:00+03:00",
            profile="multimodal",
            stops=[
                {"latitude": 55.7, "longitude": 37.5},
                {"latitude": 55.8, "longitude": 37.6},
            ],
        )


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


@pytest.mark.parametrize("profile", ["pedestrian", "bicycle"])
async def test_map_uses_road_profile_without_car_traffic(profile):
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
    assert "departure_options" in result
