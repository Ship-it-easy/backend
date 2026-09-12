from sqlalchemy.ext.asyncio import AsyncSession

from auth.entrypoint.config import PlanningServiceConfig
from planning.application.interfaces import TravelMatrixProvider
from planning.infrastructure.adapters.travel_matrix import (
    StaticTravelMatrixProvider,
    ValhallaTravelMatrixProvider,
)


class TravelMatrixProviderFactory:
    def __init__(self, session: AsyncSession, config: PlanningServiceConfig):
        self._session = session
        self._config = config

    def create(self, provider: str) -> TravelMatrixProvider:
        if provider == "STATIC_TEST":
            return StaticTravelMatrixProvider()
        if provider == "VALHALLA_LOCAL":
            return ValhallaTravelMatrixProvider(self._session, self._config)
        raise ValueError(f"Unsupported travel provider: {provider}")
