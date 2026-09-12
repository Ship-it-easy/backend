from dishka import Provider, Scope, provide

from auth.entrypoint.config import Config
from planning.application.access import ProjectAccess
from planning.application.interfaces.geocoder import Geocoder
from planning.application.interfaces.jobs_repository import JobsRepository
from planning.application.interfaces.planning_run_repository import (
    PlanningRunRepository,
)
from planning.application.interfaces.planning_solver import PlanningSolverFactory
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.geocoder_nominatim import NominatimGeocoder
from planning.infrastructure.adapters.jobs_repository_sqla import (
    SqlaJobsRepository,
)
from planning.infrastructure.adapters.planning_run_repository_sqla import (
    SqlaPlanningRunRepository,
)
from planning.infrastructure.adapters.planning_solver_ortools import (
    OrToolsPlanningSolverFactory,
)
from planning.infrastructure.adapters.travel_matrix_provider_factory import (
    TravelMatrixProviderFactory,
)
from planning.infrastructure.adapters.travel_matrix_provider_static import (
    StaticTravelMatrixProvider,
)
from planning.infrastructure.adapters.travel_matrix_provider_valhalla import (
    ValhallaTravelMatrixProvider,
)


class PlanningAdaptersProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def planning_config(self, config: Config) -> PlanningServiceConfig:
        return config.planning_service_config

    geocoder = provide(NominatimGeocoder, provides=Geocoder)
    project_access = provide(ProjectAccess)
    jobs_repository = provide(SqlaJobsRepository, provides=JobsRepository)
    planning_run_repository = provide(
        SqlaPlanningRunRepository,
        provides=PlanningRunRepository,
    )
    static_matrix_provider = provide(StaticTravelMatrixProvider)
    valhalla_matrix_provider = provide(ValhallaTravelMatrixProvider)
    matrix_factory = provide(TravelMatrixProviderFactory)
    solver_factory = provide(
        OrToolsPlanningSolverFactory,
        provides=PlanningSolverFactory,
    )
