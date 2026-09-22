import hashlib
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from planning.domain.entities.coordinate import Coordinate
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.address_search_yandex import (
    yandex_address_search_queries,
    yandex_request_params,
)
from planning.infrastructure.persistence_sqla.mappings.tables import geocoding_cache


class YandexGeocoder:
    def __init__(self, session: AsyncSession, config: PlanningServiceConfig):
        self._session = session
        self._config = config

    async def geocode(self, address: str) -> Coordinate | None:
        normalized = " ".join(address.casefold().split())
        # Keep Yandex and legacy Nominatim cache entries independent.
        address_hash = hashlib.sha256(f"yandex:{normalized}".encode()).hexdigest()
        cached = (
            (
                await self._session.execute(
                    select(geocoding_cache).where(
                        geocoding_cache.c.address_hash == address_hash
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if cached:
            return Coordinate(float(cached.latitude), float(cached.longitude))

        if not self._config.yandex_geocoder_api_key:
            raise RuntimeError("YANDEX_GEOCODER_API_KEY is required")
        async with httpx.AsyncClient(
            base_url=self._config.yandex_geocoder_url,
            timeout=self._config.geoservice_timeout_sec,
        ) as client:
            members = []
            for query in yandex_address_search_queries(address):
                response = await client.get(
                    "/v1/",
                    params=yandex_request_params(self._config, query, results=1),
                )
                response.raise_for_status()
                try:
                    members = response.json()["response"]["GeoObjectCollection"][
                        "featureMember"
                    ]
                except (KeyError, TypeError) as error:
                    raise RuntimeError(
                        "Unexpected Yandex Geocoder response"
                    ) from error
                if members:
                    break

        try:
            if not members:
                return None
            longitude, latitude = map(
                float, members[0]["GeoObject"]["Point"]["pos"].split()
            )
            coordinate = Coordinate(latitude, longitude)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise RuntimeError("Unexpected Yandex Geocoder response") from error

        await self._session.execute(
            insert(geocoding_cache)
            .values(
                address_hash=address_hash,
                normalized_address=normalized,
                latitude=Decimal(str(coordinate.latitude)),
                longitude=Decimal(str(coordinate.longitude)),
                provider="YANDEX",
                provider_version="geocoder-v1",
            )
            .on_conflict_do_nothing(index_elements=["address_hash"])
        )
        await self._session.commit()
        return coordinate
