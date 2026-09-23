import re
from typing import Any

import httpx

from planning.application.errors import PlanningUnavailable
from planning.application.interfaces.project_management_repositories import (
    AddressSearchProvider,
)
from planning.entrypoint.config import PlanningServiceConfig

_APARTMENT_SUFFIX = re.compile(r",?\s*кв\.?\s*[\w/-]+\s*$", re.IGNORECASE)
_MOSCOW_PREFIX = re.compile(
    r"^\s*(?:г\.\s*)?(?:город\s+)?москва\s*,?\s*", re.IGNORECASE
)
_ADDRESS_ABBREVIATIONS = (
    (re.compile(r"\bпр-кт\.?\s*", re.IGNORECASE), "проспект "),
    (re.compile(r"\bпросп\.\s*", re.IGNORECASE), "проспект "),
    (re.compile(r"\bул\.?\s*", re.IGNORECASE), "улица "),
    (re.compile(r"\bпер\.?\s*", re.IGNORECASE), "переулок "),
    (re.compile(r"\bб-р\.?\s*", re.IGNORECASE), "бульвар "),
    (re.compile(r"\bнаб\.?\s*", re.IGNORECASE), "набережная "),
    (re.compile(r"\bпр-д\.?\s*", re.IGNORECASE), "проезд "),
    (re.compile(r"\bпроезд\.\s*", re.IGNORECASE), "проезд "),
    (re.compile(r"\bд(?:\.\s*|\s+)(?=\d|к\d)", re.IGNORECASE), "дом "),
)


def address_search_queries(address: str) -> tuple[str, ...]:
    """Return the source address and a Nominatim-friendly fallback query.

    The CSV export uses abbreviated Russian address parts and apartment numbers.
    Apartments are not routing locations and often prevent matching a building.
    """

    source = " ".join(address.split())
    normalized = _APARTMENT_SUFFIX.sub("", source)
    normalized = _MOSCOW_PREFIX.sub("Москва, ", normalized)
    for pattern, replacement in _ADDRESS_ABBREVIATIONS:
        normalized = pattern.sub(replacement, normalized)
    normalized = re.sub(r"\s*,\s*", ", ", normalized).strip(" ,")
    # OSM's Russian street names are indexed as “Волгоградский проспект”,
    # while the export writes “пр-кт.Волгоградский”.
    normalized = re.sub(
        r"^(.*?,\s*)(проспект|улица|переулок|бульвар|набережная|проезд)\s+([^,]+)",
        r"\1\3 \2",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"\bдом\s+", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(
        r"\s+стр\.?\s*(\d+)", r" с\1", normalized, flags=re.IGNORECASE
    )
    without_structure = re.sub(r"\s+с\d+\b", "", normalized, flags=re.IGNORECASE)
    base_house = re.sub(
        r"(\d+)[а-яa-z]+(?=\s*$)", r"\1", without_structure, flags=re.IGNORECASE
    )
    # Try the normalized form first: the original export wording often returns
    # a POI, an entrance and the building as separate search results.
    return tuple(
        dict.fromkeys(
            query
            for query in (normalized, without_structure, base_house, source)
            if query
        )
    )


def _search_results(
    items: list[dict[str, Any]], *, require_house: bool
) -> list[dict[str, Any]]:
    """Collapse OSM building, entrance and POI records for the same address."""

    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        address = item.get("address", {})
        house_number = str(address.get("house_number") or "").casefold()
        building = str(address.get("building") or "").casefold()
        road = str(
            address.get("road")
            or address.get("pedestrian")
            or address.get("street")
            or address.get("locality")
            or ""
        ).casefold()
        city = str(address.get("city") or address.get("town") or "").casefold()
        if not road or (require_house and not (house_number or building)):
            continue
        key = (house_number or building, road, city)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "display_name": item["display_name"],
                "latitude": float(item["lat"]),
                "longitude": float(item["lon"]),
                "address": address,
                "address_key": str(item.get("place_id") or item.get("osm_id") or ""),
            }
        )
    # A Moscow query can still return a similarly named street in a settlement
    # administratively included in Moscow. Prefer the city itself when present.
    moscow_results = [
        candidate
        for candidate in result
        if str(
            (candidate.get("address") or {}).get("city")
            or (candidate.get("address") or {}).get("town")
            or ""
        ).casefold()
        == "москва"
    ]
    if moscow_results:
        return moscow_results
    return result


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
                for candidate in address_search_queries(query):
                    response = await client.get(
                        "/search",
                        params={
                            "q": candidate,
                            "format": "jsonv2",
                            "limit": 10,
                            "countrycodes": "ru",
                            "viewbox": self._config.nominatim_viewbox,
                            "bounded": 1,
                            "addressdetails": 1,
                        },
                    )
                    response.raise_for_status()
                    result = _search_results(
                        response.json(), require_house=require_house
                    )
                    if result:
                        return result
        except (httpx.HTTPError, TypeError, ValueError) as error:
            raise PlanningUnavailable(
                "Address suggestions are temporarily unavailable",
                code="ADDRESS_PROVIDER_UNAVAILABLE",
            ) from error

        return []
