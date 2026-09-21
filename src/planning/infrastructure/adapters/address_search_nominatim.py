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

    async def search(
        self, query: str, *, require_house: bool = True
    ) -> list[dict[str, Any]]:
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
                payload = response.json()
        except (httpx.HTTPError, TypeError, ValueError) as error:
            raise PlanningUnavailable(
                "Address suggestions are temporarily unavailable",
                code="ADDRESS_PROVIDER_UNAVAILABLE",
            ) from error

        result = []
        try:
            for item in payload:
                address = item.get("address", {})
                if require_house and not any(
                    address.get(key) for key in ("house_number", "building")
                ):
                    continue
                osm_id = item.get("osm_id")
                place_id = item.get("place_id")
                if osm_id is not None:
                    address_key = f"{item.get('osm_type') or 'osm'}:{osm_id}"
                elif place_id is not None:
                    address_key = f"place:{place_id}"
                else:
                    address_key = item["display_name"].strip().casefold()
                result.append(
                    {
                        "display_name": item["display_name"],
                        "latitude": float(item["lat"]),
                        "longitude": float(item["lon"]),
                        "address": address,
                        "address_key": address_key,
                    }
                )
        except (KeyError, TypeError, ValueError) as error:
            raise PlanningUnavailable(
                "Address suggestions are temporarily unavailable",
                code="ADDRESS_PROVIDER_UNAVAILABLE",
            ) from error
        return result
