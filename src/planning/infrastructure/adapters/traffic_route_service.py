"""Valhalla-backed implementation of the dated route boundary."""

from typing import Any

from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.traffic_route import build_traffic_route


class ValhallaTrafficRouteService:
    def __init__(self, config: PlanningServiceConfig):
        self.config = config

    async def build(self, request: Any) -> dict:
        return await build_traffic_route(request, self.config)
