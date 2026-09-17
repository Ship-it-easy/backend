from dishka import Provider, Scope, provide

from planning.application.interactors.admin import projects as admin_projects
from planning.application.interactors.admin import users as admin_users
from planning.application.interactors.create_job import CreateJobInteractor
from planning.application.interactors.dynamic_planning import (
    GetCurrentProjectPlanInteractor,
    GetPlanningEventInteractor,
    GetProjectPlanVersionInteractor,
    ListProjectPlanVersionsInteractor,
    StartDynamicPlanningInteractor,
)
from planning.application.interactors.engineer import (
    assignments as engineer_assignments,
)
from planning.application.interactors.get_planning_run import GetPlanningRunInteractor
from planning.application.interactors.job_import import ProjectJobImportInteractor
from planning.application.interactors.job_status.change_job_status import (
    ChangeJobStatusInteractor,
)
from planning.application.interactors.list_jobs import ListJobsInteractor
from planning.application.interactors.list_planning_runs import (
    ListPlanningRunsInteractor,
)
from planning.application.interactors.planning_batches import (
    GetPlanningBatchContextInteractor,
    GetPlanningBatchInteractor,
    ListPlanningBatchesInteractor,
    StartPlanningBatchInteractor,
    StopPlanningBatchInteractor,
    ValidateCurrentBatchDayInteractor,
)
from planning.application.interactors.project import address_search, engineer_access
from planning.application.interactors.project import catalogs as project_catalogs
from planning.application.interactors.project import engineers as project_engineers
from planning.application.interactors.project import jobs as project_jobs
from planning.application.interactors.project import planning as project_planning
from planning.application.interactors.start_planning_run import (
    StartPlanningRunInteractor,
)
from planning.application.services.planning_input_normalizer import (
    PlanningInputNormalizer,
)
from planning.application.validators.planning_batch import PlanningBatchValidator
from planning.application.validators.planning_result import PlanningValidator


class PlanningInteractorProvider(Provider):
    scope = Scope.REQUEST

    normalizer = provide(PlanningInputNormalizer)
    validator = provide(PlanningValidator)
    batch_validator = provide(PlanningBatchValidator)
    create_job = provide(CreateJobInteractor)
    project_job_import = provide(ProjectJobImportInteractor)
    start_dynamic_planning = provide(StartDynamicPlanningInteractor)
    get_planning_event = provide(GetPlanningEventInteractor)
    get_current_project_plan = provide(GetCurrentProjectPlanInteractor)
    list_project_plan_versions = provide(ListProjectPlanVersionsInteractor)
    get_project_plan_version = provide(GetProjectPlanVersionInteractor)
    list_jobs = provide(ListJobsInteractor)
    start_planning_run = provide(StartPlanningRunInteractor)
    get_planning_run = provide(GetPlanningRunInteractor)
    list_planning_runs = provide(ListPlanningRunsInteractor)
    change_job_status = provide(ChangeJobStatusInteractor)
    start_planning_batch = provide(StartPlanningBatchInteractor)
    get_planning_batch_context = provide(GetPlanningBatchContextInteractor)
    get_planning_batch = provide(GetPlanningBatchInteractor)
    list_planning_batches = provide(ListPlanningBatchesInteractor)
    stop_planning_batch = provide(StopPlanningBatchInteractor)
    validate_current_batch_day = provide(ValidateCurrentBatchDayInteractor)

    list_admin_projects = provide(admin_projects.ListAdminProjectsInteractor)
    create_admin_project = provide(admin_projects.CreateAdminProjectInteractor)
    get_admin_project = provide(admin_projects.GetAdminProjectInteractor)
    update_admin_project = provide(admin_projects.UpdateAdminProjectInteractor)
    block_project = provide(admin_projects.BlockProjectInteractor)
    unblock_project = provide(admin_projects.UnblockProjectInteractor)

    list_owners = provide(admin_users.ListOwnersInteractor)
    create_owner = provide(admin_users.CreateOwnerInteractor)
    list_project_users = provide(admin_users.ListProjectUsersInteractor)
    create_project_user = provide(admin_users.CreateProjectUserInteractor)
    reset_user_password = provide(admin_users.ResetUserPasswordInteractor)
    block_user = provide(admin_users.BlockUserInteractor)
    unblock_user = provide(admin_users.UnblockUserInteractor)

    list_assignments = provide(engineer_assignments.ListAssignmentsInteractor)
    get_route_state = provide(engineer_assignments.GetRouteStateInteractor)
    get_assignment = provide(engineer_assignments.GetAssignmentInteractor)
    start_assignment = provide(engineer_assignments.StartAssignmentInteractor)
    complete_assignment = provide(engineer_assignments.CompleteAssignmentInteractor)
    return_assignment = provide(engineer_assignments.ReturnAssignmentInteractor)

    list_qualifications = provide(project_catalogs.ListQualificationsInteractor)
    create_qualification = provide(project_catalogs.CreateQualificationInteractor)
    update_qualification = provide(project_catalogs.UpdateQualificationInteractor)
    list_equipment_types = provide(project_catalogs.ListEquipmentTypesInteractor)
    create_equipment_type = provide(project_catalogs.CreateEquipmentTypeInteractor)
    update_equipment_type = provide(project_catalogs.UpdateEquipmentTypeInteractor)
    clear_equipment = provide(project_catalogs.ClearEquipmentQuantityInteractor)
    list_work_types = provide(project_catalogs.ListWorkTypesInteractor)
    create_work_type = provide(project_catalogs.CreateWorkTypeInteractor)
    update_work_type = provide(project_catalogs.UpdateWorkTypeInteractor)

    list_engineers = provide(project_engineers.ListEngineersInteractor)
    create_engineer = provide(project_engineers.CreateEngineerInteractor)
    get_engineer = provide(project_engineers.GetEngineerInteractor)
    update_engineer = provide(project_engineers.UpdateEngineerInteractor)
    replace_schedule = provide(project_engineers.ReplaceEngineerScheduleInteractor)

    create_engineer_access = provide(engineer_access.CreateEngineerAccessInteractor)
    reset_engineer_password = provide(engineer_access.ResetEngineerPasswordInteractor)
    block_engineer_access = provide(engineer_access.BlockEngineerAccessInteractor)
    unblock_engineer_access = provide(engineer_access.UnblockEngineerAccessInteractor)

    list_project_jobs = provide(project_jobs.ListProjectJobsInteractor)
    create_project_job = provide(project_jobs.CreateProjectJobInteractor)
    get_project_job = provide(project_jobs.GetProjectJobInteractor)
    update_project_job = provide(project_jobs.UpdateProjectJobInteractor)
    change_project_job_status = provide(project_jobs.ChangeProjectJobStatusInteractor)

    search_addresses = provide(address_search.SearchAddressesInteractor)
    get_planning_config = provide(project_planning.GetPlanningConfigInteractor)
    update_planning_config = provide(project_planning.UpdatePlanningConfigInteractor)
    check_planning_readiness = provide(
        project_planning.CheckPlanningReadinessInteractor
    )
    publish_planning_run = provide(project_planning.PublishPlanningRunInteractor)
    get_daily_plan = provide(project_planning.GetDailyPlanInteractor)
