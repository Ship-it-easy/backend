from dishka import Provider, Scope, provide

from auth.entrypoint.config import Config
from planning.application.access import ProjectAccess
from planning.application.interfaces.admin_management_repositories import (
    AdminProjectRepository,
    AdminUserRepository,
)
from planning.application.interfaces.assignment_repository import AssignmentRepository
from planning.application.interfaces.geocoder import Geocoder
from planning.application.interfaces.job_status_repository import JobStatusRepository
from planning.application.interfaces.jobs_repository import JobsRepository
from planning.application.interfaces.planning_run_repository import (
    PlanningRunRepository,
)
from planning.application.interfaces.planning_solver import PlanningSolverFactory
from planning.application.interfaces.project_access_repository import (
    ProjectAccessRepository,
)
from planning.application.interfaces.project_management_repositories import (
    AddressSearchProvider,
    EngineerAccountRepository,
    EngineerManagementRepository,
    PlanningManagementRepository,
    ProjectCatalogRepository,
    ProjectJobsRepository,
)
from planning.application.interfaces.unit_of_work import PlanningUnitOfWork
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.address_search_nominatim import (
    NominatimAddressSearchProvider,
)
from planning.infrastructure.adapters.admin_management_repositories_sqla import (
    SqlaAdminProjectRepository,
    SqlaAdminUserRepository,
)
from planning.infrastructure.adapters.assignment_repository_sqla import (
    SqlaAssignmentRepository,
)
from planning.infrastructure.adapters.geocoder_nominatim import NominatimGeocoder
from planning.infrastructure.adapters.job_status_repository_sqla import (
    SqlaJobStatusRepository,
)
from planning.infrastructure.adapters.jobs_repository_sqla import (
    SqlaJobsRepository,
)
from planning.infrastructure.adapters.planning_management_repository_sqla import (
    SqlaPlanningManagementRepository,
)
from planning.infrastructure.adapters.planning_run_repository_sqla import (
    SqlaPlanningRunRepository,
)
from planning.infrastructure.adapters.planning_solver_ortools import (
    OrToolsPlanningSolverFactory,
)
from planning.infrastructure.adapters.project_access_repository_sqla import (
    SqlaProjectAccessRepository,
)
from planning.infrastructure.adapters.project_management_repositories_sqla import (
    SqlaEngineerAccountRepository,
    SqlaEngineerManagementRepository,
    SqlaProjectCatalogRepository,
    SqlaProjectJobsRepository,
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
from planning.infrastructure.adapters.unit_of_work_sqla import SqlaPlanningUnitOfWork


class PlanningAdaptersProvider(Provider):
    scope = Scope.REQUEST

    @provide
    def planning_config(self, config: Config) -> PlanningServiceConfig:
        return config.planning_service_config

    geocoder = provide(NominatimGeocoder, provides=Geocoder)
    project_access = provide(ProjectAccess)
    unit_of_work = provide(
        SqlaPlanningUnitOfWork,
        provides=PlanningUnitOfWork,
    )
    project_access_repository = provide(
        SqlaProjectAccessRepository,
        provides=ProjectAccessRepository,
    )
    job_status_repository = provide(
        SqlaJobStatusRepository,
        provides=JobStatusRepository,
    )
    admin_project_repository = provide(
        SqlaAdminProjectRepository,
        provides=AdminProjectRepository,
    )
    admin_user_repository = provide(
        SqlaAdminUserRepository,
        provides=AdminUserRepository,
    )
    assignment_repository = provide(
        SqlaAssignmentRepository,
        provides=AssignmentRepository,
    )
    project_catalog_repository = provide(
        SqlaProjectCatalogRepository,
        provides=ProjectCatalogRepository,
    )
    engineer_management_repository = provide(
        SqlaEngineerManagementRepository,
        provides=EngineerManagementRepository,
    )
    engineer_account_repository = provide(
        SqlaEngineerAccountRepository,
        provides=EngineerAccountRepository,
    )
    project_jobs_repository = provide(
        SqlaProjectJobsRepository,
        provides=ProjectJobsRepository,
    )
    planning_management_repository = provide(
        SqlaPlanningManagementRepository,
        provides=PlanningManagementRepository,
    )
    address_search_provider = provide(
        NominatimAddressSearchProvider,
        provides=AddressSearchProvider,
    )
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
