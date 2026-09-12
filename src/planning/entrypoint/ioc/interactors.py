from dishka import Provider, Scope, provide

from planning.application.interactors.create_job import CreateJobInteractor
from planning.application.interactors.get_planning_run import GetPlanningRunInteractor
from planning.application.interactors.list_jobs import ListJobsInteractor
from planning.application.interactors.list_planning_runs import (
    ListPlanningRunsInteractor,
)
from planning.application.interactors.start_planning_run import (
    StartPlanningRunInteractor,
)
from planning.application.services.planning_input_normalizer import (
    PlanningInputNormalizer,
)
from planning.application.validators.planning_result import PlanningValidator


class PlanningInteractorProvider(Provider):
    scope = Scope.REQUEST

    normalizer = provide(PlanningInputNormalizer)
    validator = provide(PlanningValidator)
    create_job = provide(CreateJobInteractor)
    list_jobs = provide(ListJobsInteractor)
    start_planning_run = provide(StartPlanningRunInteractor)
    get_planning_run = provide(GetPlanningRunInteractor)
    list_planning_runs = provide(ListPlanningRunsInteractor)
