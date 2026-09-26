from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncSession

from auth.entrypoint.config import Config
from planning.application.access import ProjectAccess
from planning.application.interfaces.admin_management_repositories import (
    AdminProjectRepository,
    AdminUserRepository,
)
from planning.application.interfaces.assignment_repository import AssignmentRepository
from planning.application.interfaces.baseline_retry import BaselineRetryOperations
from planning.application.interfaces.dynamic_planning_repository import (
    DynamicPlanningRepository,
)
from planning.application.interfaces.geocoder import Geocoder
from planning.application.interfaces.job_imports import JobImportOperations
from planning.application.interfaces.job_status_repository import JobStatusRepository
from planning.application.interfaces.jobs_repository import JobsRepository
from planning.application.interfaces.planning_batch_repository import (
    PlanningBatchExecutor,
    PlanningBatchRepository,
)
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
from planning.application.interfaces.traffic_route_service import TrafficRouteService
from planning.application.interfaces.transaction_manager import TransactionManager
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.admin_management_repositories_sqla import (
    SqlaAdminProjectRepository,
    SqlaAdminUserRepository,
)
from planning.infrastructure.adapters.assignment_repository_sqla import (
    SqlaAssignmentRepository,
)
from planning.infrastructure.adapters.baseline_retry_sqla import (
    SqlaBaselineRetryOperations,
)
from planning.infrastructure.adapters.dynamic_planning_repository_sqla import (
    SqlaDynamicPlanningRepository,
)
from planning.infrastructure.adapters.geocoder_factory import (
    create_address_search_provider,
    create_geocoder,
)
from planning.infrastructure.adapters.job_import_csv import (
    JobImportExecutor,
    JobImportService,
)
from planning.infrastructure.adapters.job_status_repository_sqla import (
    SqlaJobStatusRepository,
)
from planning.infrastructure.adapters.jobs_repository_sqla import (
    SqlaJobsRepository,
)
from planning.infrastructure.adapters.planning_batch_executor import (
    InProcessPlanningBatchExecutor,
)
from planning.infrastructure.adapters.planning_batch_repository_sqla import (
    SqlaPlanningBatchRepository,
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
from planning.infrastructure.adapters.traffic_route_service import (
    ValhallaTrafficRouteService,
)
from planning.infrastructure.adapters.transaction_manager_sqla import (
    SqlAlchemyTransactionManager,
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
    job_import_executor = provide(JobImportExecutor, scope=Scope.APP)
    job_import_service = provide(JobImportService, provides=JobImportOperations)

    @provide(scope=Scope.APP)
    def planning_config(self, config: Config) -> PlanningServiceConfig:
        return config.planning_service_config

    @provide
    def geocoder(
        self, session: AsyncSession, config: PlanningServiceConfig
    ) -> Geocoder:
        return create_geocoder(session, config)
    dynamic_planning_repository = provide(
        SqlaDynamicPlanningRepository,
        provides=DynamicPlanningRepository,
    )
    baseline_retry_operations = provide(
        SqlaBaselineRetryOperations,
        provides=BaselineRetryOperations,
    )
    project_access = provide(ProjectAccess)
    transaction_manager = provide(
        SqlAlchemyTransactionManager,
        provides=TransactionManager,
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
    @provide
    def address_search_provider(
        self, config: PlanningServiceConfig
    ) -> AddressSearchProvider:
        return create_address_search_provider(config)
    jobs_repository = provide(SqlaJobsRepository, provides=JobsRepository)
    planning_run_repository = provide(
        SqlaPlanningRunRepository,
        provides=PlanningRunRepository,
    )
    planning_batch_repository = provide(
        SqlaPlanningBatchRepository,
        provides=PlanningBatchRepository,
    )
    planning_batch_executor = provide(
        InProcessPlanningBatchExecutor,
        scope=Scope.APP,
        provides=PlanningBatchExecutor,
    )
    static_matrix_provider = provide(StaticTravelMatrixProvider)
    valhalla_matrix_provider = provide(ValhallaTravelMatrixProvider)
    traffic_route_service = provide(
        ValhallaTrafficRouteService, provides=TrafficRouteService
    )
    matrix_factory = provide(TravelMatrixProviderFactory)
    @provide
    def solver_factory(
        self,
        matrix_factory: TravelMatrixProviderFactory,
        config: PlanningServiceConfig,
    ) -> PlanningSolverFactory:
        return OrToolsPlanningSolverFactory(
            matrix_factory,
            config.baseline_comparison_enabled,
        )
