from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from planning.domain.entities.coordinate import Coordinate
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.travel_matrix_provider_valhalla import (
    ValhallaTravelMatrixProvider,
)


@pytest.mark.parametrize(
    "http_status,error_code,unreachable",
    [(400, 441, True), (400, 442, True), (500, 499, False), (400, 430, False)],
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
