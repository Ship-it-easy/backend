from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.interfaces.unit_of_work import PlanningUnitOfWork


class SqlaPlanningUnitOfWork(PlanningUnitOfWork):
    """Request-scoped transaction boundary over the shared SQLAlchemy session."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def __aenter__(self) -> "SqlaPlanningUnitOfWork":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session.in_transaction():
            await self._session.rollback()

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
