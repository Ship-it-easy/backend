from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.interfaces.geocoder import Geocoder
from planning.application.interfaces.project_management_repositories import (
    AddressSearchProvider,
)
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.address_search_nominatim import (
    NominatimAddressSearchProvider,
)
from planning.infrastructure.adapters.address_search_yandex import (
    YandexAddressSearchProvider,
)
from planning.infrastructure.adapters.geocoder_nominatim import NominatimGeocoder
from planning.infrastructure.adapters.geocoder_yandex import YandexGeocoder


def create_geocoder(
    session: AsyncSession, config: PlanningServiceConfig
) -> Geocoder:
    if config.use_yandex_geocoder:
        return YandexGeocoder(session, config)
    return NominatimGeocoder(session, config)


def create_address_search_provider(
    config: PlanningServiceConfig,
) -> AddressSearchProvider:
    if config.use_yandex_geocoder:
        return YandexAddressSearchProvider(config)
    return NominatimAddressSearchProvider(config)


def address_provider_version(config: PlanningServiceConfig) -> str:
    return "yandex-geocoder-v1" if config.use_yandex_geocoder else "nominatim-v1"
