import hashlib
import re
from typing import Any

import httpx

from planning.application.errors import PlanningUnavailable
from planning.application.interfaces.project_management_repositories import (
    AddressSearchProvider,
)
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.address_search_nominatim import (
    address_search_queries,
)

_COMPONENT_KEYS = {
    "country": "country",
    "province": "state",
    "area": "county",
    "locality": "city",
    "district": "suburb",
    "street": "road",
    "house": "house_number",
}
_PRECISION_RANK = {
    "exact": 0,
    "number": 1,
    "range": 2,
    "near": 3,
    "street": 4,
    "other": 5,
}
_DISTRICT_STOP_WORDS = {
    "административный",
    "городской",
    "микрорайон",
    "округ",
    "район",
    "посёлок",
}


def _country_code(address_data: dict[str, Any], address: dict[str, str]) -> str:
    """Return the two-letter code expected by the import validator."""

    value = str(address_data.get("country_code") or address.get("country") or "")
    normalized = value.strip().casefold()
    if normalized in {"ru", "rus", "russia", "россия"}:
        return "ru"
    return normalized


def yandex_bbox(config: PlanningServiceConfig) -> str:
    if config.yandex_geocoder_bbox:
        return config.yandex_geocoder_bbox
    try:
        left, top, right, bottom = config.nominatim_viewbox.split(",")
    except ValueError:
        return ""
    return f"{left},{bottom}~{right},{top}"


def yandex_request_params(
    config: PlanningServiceConfig, query: str, *, results: int
) -> dict[str, str | int]:
    params: dict[str, str | int] = {
        "apikey": config.yandex_geocoder_api_key,
        "geocode": query,
        "format": "json",
        "lang": "ru_RU",
        "results": results,
    }
    bbox = yandex_bbox(config)
    if bbox:
        params.update(bbox=bbox, rspn=1)
    return params


def yandex_address_search_queries(address: str) -> tuple[str, ...]:
    """Build Yandex-friendly variants without changing Nominatim queries."""

    queries = []
    for query in address_search_queries(address):
        compacted = re.sub(r"(\d)\s+к\s*(\d+\b)", r"\1к\2", query)
        queries.extend((compacted, query))
    return tuple(dict.fromkeys(queries))


def _words(value: str) -> set[str]:
    return set(re.findall(r"[\wа-яё]+", value.casefold()))


def _select_yandex_results(
    candidates: list[tuple[dict[str, Any], str]], query: str | None
) -> list[dict[str, Any]]:
    if not candidates:
        return []

    if query:
        query_words = _words(query)
        locality_matches = [
            item
            for item in candidates
            if (city := str((item[0].get("address") or {}).get("city") or ""))
            and _words(city).issubset(query_words)
        ]
        if locality_matches:
            candidates = locality_matches
    else:
        moscow_results = [
            item
            for item in candidates
            if str((item[0].get("address") or {}).get("city") or "").casefold()
            == "москва"
        ]
        if moscow_results:
            candidates = moscow_results

    query_words = _words(query or "")

    def rank(item: tuple[dict[str, Any], str]) -> tuple[int, int]:
        candidate, precision = item
        district = str((candidate.get("address") or {}).get("suburb") or "")
        district_words = _words(district) - _DISTRICT_STOP_WORDS
        district_penalty = int(
            bool(district_words and not district_words <= query_words)
        )
        return (_PRECISION_RANK.get(precision, len(_PRECISION_RANK)), district_penalty)

    best_rank = min(rank(item) for item in candidates)
    return [
        candidate
        for candidate, precision in candidates
        if rank((candidate, precision)) == best_rank
    ]


def parse_yandex_results(
    payload: Any, *, query: str | None = None, require_house: bool = True
) -> list[dict[str, Any]]:
    try:
        members = payload["response"]["GeoObjectCollection"]["featureMember"]
        result: list[tuple[dict[str, Any], str]] = []
        for member in members:
            geo_object = member["GeoObject"]
            metadata = geo_object["metaDataProperty"]["GeocoderMetaData"]
            address_data = metadata.get("Address") or {}
            components = address_data.get("Components") or []
            address = {
                key: component["name"]
                for component in components
                if (key := _COMPONENT_KEYS.get(component.get("kind")))
                and component.get("name")
            }
            address["country_code"] = _country_code(address_data, address)
            if require_house and not address.get("house_number"):
                continue
            if not address.get("road") and address.get("suburb"):
                address["locality"] = address["suburb"]
            longitude, latitude = map(float, geo_object["Point"]["pos"].split())
            display_name = (
                address_data.get("formatted")
                or metadata.get("text")
                or geo_object["name"]
            )
            source_key = geo_object.get("uri") or display_name.strip().casefold()
            result.append(
                (
                    {
                        "display_name": display_name,
                        "latitude": latitude,
                        "longitude": longitude,
                        "address": address,
                        # Yandex URIs can exceed the database VARCHAR(255) limit.
                        "address_key": hashlib.sha256(
                            source_key.encode("utf-8")
                        ).hexdigest(),
                    },
                    str(metadata.get("precision") or "other").casefold(),
                )
            )
        return _select_yandex_results(result, query)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("Unexpected Yandex Geocoder response") from error


class YandexAddressSearchProvider(AddressSearchProvider):
    def __init__(self, config: PlanningServiceConfig):
        self._config = config

    async def search(
        self, query: str, *, require_house: bool = True
    ) -> list[dict[str, Any]]:
        if not self._config.yandex_geocoder_api_key:
            raise PlanningUnavailable(
                "Yandex Geocoder API key is not configured",
                code="ADDRESS_PROVIDER_UNAVAILABLE",
            )
        try:
            async with httpx.AsyncClient(
                base_url=self._config.yandex_geocoder_url,
                timeout=self._config.geoservice_timeout_sec,
            ) as client:
                for candidate in yandex_address_search_queries(query):
                    response = await client.get(
                        "/v1/",
                        params=yandex_request_params(
                            self._config, candidate, results=10
                        ),
                    )
                    response.raise_for_status()
                    result = parse_yandex_results(
                        response.json(),
                        query=candidate,
                        require_house=require_house,
                    )
                    if result:
                        return result
        except (httpx.HTTPError, TypeError, ValueError) as error:
            raise PlanningUnavailable(
                "Address suggestions are temporarily unavailable",
                code="ADDRESS_PROVIDER_UNAVAILABLE",
            ) from error
        return []
