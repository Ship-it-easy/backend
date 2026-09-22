"""Exercise cold cache SQL at the 100 jobs / 10 engineers demo scale."""

from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from planning.domain.entities.coordinate import Coordinate
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.travel_matrix_provider_valhalla import (
    ValhallaTravelMatrixProvider,
)
from planning.infrastructure.persistence_sqla.mappings.tables import travel_time_cache
from tests.integration.test_management_postgres import TEST_DSN, pg_engine  # noqa: F401

pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_POSTGRES_DSN is required")


async def test_cold_110_point_matrix_uses_bounded_sql_and_warm_cache(pg_engine):  # noqa: F811
    requests = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, path, json):
            requests.append(path)
            return httpx.Response(
                200,
                request=httpx.Request("POST", "http://test" + path),
                json={
                    "sources_to_targets": [
                        [{"time": 60, "distance": 1.0} for _ in json["targets"]]
                        for _ in json["sources"]
                    ],
                },
            )

    coordinates = [Coordinate(58, 56 + index / 1000) for index in range(110)]
    config = PlanningServiceConfig("http://test", "http://test", "", 1, 40)
    async with AsyncSession(pg_engine) as session:
        provider = ValhallaTravelMatrixProvider(session, config)
        with patch(
            "planning.infrastructure.adapters.travel_matrix_provider_valhalla.httpx.AsyncClient",
            return_value=Client(),
        ):
            first = await provider.get_matrix(coordinates, "auto")
            assert (
                await session.scalar(
                    select(func.count()).select_from(travel_time_cache)
                )
                == 12100
            )
            assert len(requests) == 9
            second = await provider.get_matrix(coordinates, "auto")
            assert len(requests) == 9
        assert first == second
        assert first.distance_meters[109][0] == 1000
