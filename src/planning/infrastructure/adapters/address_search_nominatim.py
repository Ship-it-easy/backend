from typing import Any

import httpx

from planning.application.errors import PlanningUnavailable
from planning.application.interfaces.project_management_repositories import (
    AddressSearchProvider,
)
from planning.entrypoint.config import PlanningServiceConfig


class NominatimAddressSearchProvider(AddressSearchProvider):
    def __init__(self, config: PlanningServiceConfig):
        self._config = config

    async def search(self, query: str) -> list[dict[str, Any]]:
        try:
            async with httpx.AsyncClient(
                base_url=self._config.nominatim_url,
                timeout=self._config.geoservice_timeout_sec,
            ) as client:
                response = await client.get(
                    "/search",
                    params={
                        "q": query,
                        "format": "jsonv2",
                        "limit": 10,
                        "countrycodes": "ru",
                        "viewbox": self._config.nominatim_viewbox,
                        "bounded": 1,
                        "addressdetails": 1,
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError as error:
            raise PlanningUnavailable(
                "Address suggestions are temporarily unavailable",
                code="ADDRESS_PROVIDER_UNAVAILABLE",
            ) from error

        result = []
        for item in response.json():
            address = item.get("address", {})
            if not any(address.get(key) for key in ("house_number", "building")):
                continue
            result.append(
                {
                    "display_name": item["display_name"],
                    "latitude": float(item["lat"]),
                    "longitude": float(item["lon"]),
                }
            )
        return result
