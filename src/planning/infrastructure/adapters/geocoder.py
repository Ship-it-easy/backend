import hashlib
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from auth.entrypoint.config import PlanningServiceConfig
from planning.domain.models import Coordinate
from planning.infrastructure.persistence.tables import geocoding_cache


class NominatimGeocoder:
    def __init__(self, session: AsyncSession, config: PlanningServiceConfig):
        self._session = session
        self._config = config

    async def geocode(self, address: str) -> Coordinate | None:
        normalized = " ".join(address.casefold().split())
        address_hash = hashlib.sha256(normalized.encode()).hexdigest()
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

        async with httpx.AsyncClient(
            base_url=self._config.nominatim_url,
            timeout=self._config.geoservice_timeout_sec,
            headers={"User-Agent": "ship-it-planning/1.0"},
        ) as client:
            response = await client.get(
                "/search",
                params={
                    "q": address,
                    "format": "jsonv2",
                    "limit": 1,
                    "countrycodes": "ru",
                    "viewbox": self._config.nominatim_viewbox,
                    "bounded": 1,
                },
            )
            response.raise_for_status()
            items = response.json()
        if not items:
            return None

        coordinate = Coordinate(float(items[0]["lat"]), float(items[0]["lon"]))
        await self._session.execute(
            insert(geocoding_cache)
            .values(
                address_hash=address_hash,
                normalized_address=normalized,
                latitude=Decimal(str(coordinate.latitude)),
                longitude=Decimal(str(coordinate.longitude)),
                provider="NOMINATIM_LOCAL",
            )
            .on_conflict_do_nothing(index_elements=["address_hash"])
        )
        await self._session.commit()
        return coordinate
