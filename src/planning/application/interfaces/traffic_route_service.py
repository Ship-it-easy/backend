"""Application boundary for a dated map route."""

from typing import Any, Protocol


class TrafficRouteService(Protocol):
    async def build(self, request: Any) -> dict: ...
