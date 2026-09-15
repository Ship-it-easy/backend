from planning.application.interfaces.travel_matrix_provider import TravelMatrixProvider
from planning.infrastructure.adapters.travel_matrix_provider_static import (
    StaticTravelMatrixProvider,
)
from planning.infrastructure.adapters.travel_matrix_provider_valhalla import (
    ValhallaTravelMatrixProvider,
)


class TravelMatrixProviderFactory:
    def __init__(
        self,
        static_provider: StaticTravelMatrixProvider,
        valhalla_provider: ValhallaTravelMatrixProvider,
    ):
        self._static_provider = static_provider
        self._valhalla_provider = valhalla_provider

    def create(self, provider: str) -> TravelMatrixProvider:
        if provider == "STATIC_TEST":
            return self._static_provider
        if provider == "VALHALLA_LOCAL":
            return self._valhalla_provider
        raise ValueError(f"Unsupported travel provider: {provider}")
