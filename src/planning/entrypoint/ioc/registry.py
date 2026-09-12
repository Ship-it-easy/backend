from collections.abc import Iterable

from dishka import Provider

from planning.entrypoint.ioc.adapters import PlanningAdaptersProvider
from planning.entrypoint.ioc.interactors import PlanningInteractorProvider


def get_planning_providers() -> Iterable[Provider]:
    return (
        PlanningAdaptersProvider(),
        PlanningInteractorProvider(),
    )
