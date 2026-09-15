from typing import Iterable

from dishka import Provider

from auth.entrypoint.ioc.adapters import (
    AuthProvider,
    ConfigProvider,
    IdGeneratorsProvider,
    SqlaProvider,
)
from auth.entrypoint.ioc.interactors import InteractorProvider
from planning.entrypoint.ioc.registry import get_planning_providers


def get_providers() -> Iterable[Provider]:
    return (
        InteractorProvider(),
        SqlaProvider(),
        IdGeneratorsProvider(),
        AuthProvider(),
        ConfigProvider(),
        *get_planning_providers(),
    )
