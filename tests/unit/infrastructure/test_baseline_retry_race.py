from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from planning.application.errors import ConflictError
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.planning_batch_executor import (
    InProcessPlanningBatchExecutor,
)


async def test_background_retry_exits_when_another_worker_exhausts_attempts():
    row = SimpleNamespace(
        status="PENDING",
        attempt_count=2,
        next_retry_at=None,
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _query):
            result = MagicMock()
            result.mappings.return_value.one_or_none.return_value = row
            return result

    executor = object.__new__(InProcessPlanningBatchExecutor)
    executor._sessionmaker = Session
    executor._config = PlanningServiceConfig("http://test", "http://test", "", 1, 40)
    conflict = ConflictError(
        "Retry limit exceeded", code="BASELINE_RETRY_LIMIT_EXCEEDED"
    )
    with patch(
        "planning.infrastructure.adapters.planning_batch_executor.SqlaBaselineRetryOperations"
    ) as operations:
        operations.return_value.run_queued = AsyncMock(side_effect=conflict)
        await executor._retry_baseline_until_terminal(11, 115)

    operations.return_value.run_queued.assert_awaited_once_with(11, 115)
