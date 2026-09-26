from dishka import Provider, Scope, provide

from auth.application.interactors.log_in import LogInInteractor
from auth.application.interactors.log_out import LogOutInteractor


class InteractorProvider(Provider):
    scope = Scope.REQUEST

    log_in = provide(LogInInteractor)
    log_out = provide(LogOutInteractor)
