from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from planning.domain.entities.coordinate import Coordinate
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.travel_matrix_provider_valhalla import (
    ValhallaTravelMatrixProvider,
    _cache_key,
)


async def test_cached_unreachable_pair_does_not_retry_valhalla():
    points = [Coordinate(58, 56), Coordinate(58.01, 56.01)]
    cached = [
        SimpleNamespace(
            cache_key=_cache_key(origin, destination, "pedestrian"),
            travel_time_seconds=0 if i == j else None,
            distance_meters=0 if i == j else None,
        )
        for i, origin in enumerate(points)
        for j, destination in enumerate(points)
    ]
    session = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value = cached
    session.execute.return_value = rows
    client = AsyncMock()
    client.__aenter__.return_value = client
    provider = ValhallaTravelMatrixProvider(
        session, PlanningServiceConfig("http://test", "http://test", "", 1, 40)
    )
    with patch(
        "planning.infrastructure.adapters.travel_matrix_provider_valhalla.httpx.AsyncClient",
        return_value=client,
    ):
        result = await provider.get_matrix(points, "pedestrian")
    assert result.travel_time_seconds == [[0, None], [None, 0]]
    client.post.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "http_status,error_code,unreachable",
    [
        (400, 170, True),
        (400, 171, True),
        (400, 441, True),
        (400, 442, True),
        (500, 499, False),
        (400, 430, False),
    ],
)
async def test_route_fallback_distinguishes_unreachable_from_provider_failure(
    http_status, error_code, unreachable
):
    session = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value = []
    session.execute.return_value = rows

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, path, json):
            request = httpx.Request("POST", "http://test" + path)
            if path == "/sources_to_targets":
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "sources_to_targets": [
                            [{"time": 0, "distance": 0}, None],
                            [None, {"time": 0, "distance": 0}],
                        ]
                    },
                )
            return httpx.Response(
                http_status, request=request, json={"error_code": error_code}
            )

    provider = ValhallaTravelMatrixProvider(
        session, PlanningServiceConfig("http://test", "http://test", "", 1, 40)
    )
    with patch(
        "planning.infrastructure.adapters.travel_matrix_provider_valhalla.httpx.AsyncClient",
        return_value=Client(),
    ):
        if unreachable:
            matrix = await provider.get_matrix(
                [Coordinate(58, 56), Coordinate(58.01, 56.01)], "auto"
            )
            assert matrix.travel_time_seconds[0][1] is None
            assert matrix.distance_meters[0][1] is None
        else:
            with pytest.raises(httpx.HTTPStatusError):
                await provider.get_matrix(
                    [Coordinate(58, 56), Coordinate(58.01, 56.01)], "auto"
                )
            session.commit.assert_not_awaited()


async def test_bad_matrix_point_does_not_force_healthy_pairs_through_route():
    session = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value = []
    session.execute.return_value = rows
    route_pairs = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, path, json):
            request = httpx.Request("POST", "http://test" + path)
            if path == "/sources_to_targets":
                if any(
                    point["lat"] == 56 for point in json["sources"] + json["targets"]
                ):
                    return httpx.Response(
                        400, request=request, json={"error_code": 442}
                    )
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "sources_to_targets": [
                            [{"time": 90, "distance": 0.5} for _ in json["targets"]]
                            for _ in json["sources"]
                        ]
                    },
                )
            route_pairs.append(tuple(point["lat"] for point in json["locations"]))
            return httpx.Response(400, request=request, json={"error_code": 442})

    provider = ValhallaTravelMatrixProvider(
        session, PlanningServiceConfig("http://test", "http://test", "", 1, 40)
    )
    with patch(
        "planning.infrastructure.adapters.travel_matrix_provider_valhalla.httpx.AsyncClient",
        return_value=Client(),
    ):
        matrix = await provider.get_matrix(
            [Coordinate(55.7, 37.5), Coordinate(55.8, 37.6), Coordinate(56, 37.7)],
            "pedestrian",
        )
    assert matrix.travel_time_seconds[0][1] == 90
    assert (55.7, 55.8) not in route_pairs
    assert (55.8, 55.7) not in route_pairs


async def test_required_fifo_arc_bypasses_candidate_limit():
    points = [
        Coordinate(55.7, 37.5),
        Coordinate(55.7000004, 37.5000004),
        Coordinate(55.701, 37.501),
        Coordinate(55.8, 37.6),
    ]
    required = {
        (
            "auto",
            points[1].latitude,
            points[1].longitude,
            points[3].latitude,
            points[3].longitude,
        )
    }
    session = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value = []
    session.execute.return_value = rows

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, path, json):
            assert path == "/sources_to_targets"
            request = httpx.Request("POST", "http://test" + path)
            return httpx.Response(
                200,
                request=request,
                json={
                    "sources_to_targets": [
                        [{"time": 60, "distance": 1} for _ in json["targets"]]
                        for _ in json["sources"]
                    ]
                },
            )

    provider = ValhallaTravelMatrixProvider(
        session,
        PlanningServiceConfig(
            "http://test", "http://test", "", 1, 40, matrix_candidate_limit=1
        ),
    )
    with patch(
        "planning.infrastructure.adapters.travel_matrix_provider_valhalla.httpx.AsyncClient",
        return_value=Client(),
    ):
        ordinary = await provider.get_matrix(points, "auto")
        required_matrix = await provider.get_required_matrix(
            points, "auto", None, required
        )
    assert ordinary.distance_meters[1][3] is None
    assert required_matrix.distance_meters[1][3] == 1000
    assert required_matrix.distance_meters[1][2] is None


async def test_incomplete_route_response_is_not_cached_as_no_route():
    session = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value = []
    session.execute.return_value = rows

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, path, json):
            request = httpx.Request("POST", "http://test" + path)
            if path == "/sources_to_targets":
                return httpx.Response(
                    200,
                    request=request,
                    json={
                        "sources_to_targets": [
                            [None for _ in json["targets"]]
                            for _ in json["sources"]
                        ]
                    },
                )
            return httpx.Response(200, request=request, json={"trip": {}})

    provider = ValhallaTravelMatrixProvider(
        session, PlanningServiceConfig("http://test", "http://test", "", 1, 40)
    )
    with patch(
        "planning.infrastructure.adapters.travel_matrix_provider_valhalla.httpx.AsyncClient",
        return_value=Client(),
    ):
        with pytest.raises(ValueError, match="INVALID_TRAVEL_MATRIX_RESPONSE"):
            await provider.get_matrix(
                [Coordinate(55.7, 37.5), Coordinate(55.8, 37.6)], "auto"
            )
    session.commit.assert_not_awaited()
