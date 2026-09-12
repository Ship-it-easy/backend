from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from auth.entrypoint.config import Config, PlanningServiceConfig
from planning.application.interfaces import Geocoder, PlanningRunRepository
from planning.application.normalizer import PlanningInputNormalizer
from planning.application.service import PlanningService
from planning.application.validator import PlanningValidator
from planning.infrastructure.adapters.geocoder import NominatimGeocoder
from planning.infrastructure.adapters.travel_factory import (
    TravelMatrixProviderFactory,
)
from planning.infrastructure.persistence.repository import (
    SqlaPlanningRunRepository,
)


class PlanningProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def planning_config(self, config: Config) -> PlanningServiceConfig:
        return config.planning_service_config

    @provide
    def geocoder(
        self, session: AsyncSession, config: PlanningServiceConfig
    ) -> Geocoder:
        return NominatimGeocoder(session, config)

    repository = provide(SqlaPlanningRunRepository, provides=PlanningRunRepository)
    normalizer = provide(PlanningInputNormalizer)
    matrix_factory = provide(TravelMatrixProviderFactory)
    validator = provide(PlanningValidator)
    service = provide(PlanningService)
