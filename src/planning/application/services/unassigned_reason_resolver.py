from planning.domain.entities.job import Job
from planning.domain.entities.planning import PlanningInput
from planning.domain.enums import ReasonCode, TransportType


class UnassignedReasonResolver:
    """Resolve only deterministic causes without additional solver runs."""

    def resolve_optimizer_drop(
        self,
        job: Job,
        data: PlanningInput,
        matrices: dict[str, list[list[int | None]]],
        compatible_vehicles: list[int],
    ) -> ReasonCode:
        target = next(
            index for index, value in enumerate(data.jobs) if value.id == job.id
        )
        for vehicle in compatible_vehicles:
            engineer = data.engineers[vehicle]
            profile = (
                "auto" if engineer.transport_type == TransportType.CAR else "pedestrian"
            )
            matrix = matrices[profile]
            start_node = len(data.jobs) + vehicle
            if matrix[start_node][target] is not None:
                return ReasonCode.NOT_SELECTED_BY_OPTIMIZER
            if any(
                matrix[source][target] is not None
                for source in range(len(data.jobs))
                if source != target
            ):
                return ReasonCode.NOT_SELECTED_BY_OPTIMIZER
        return ReasonCode.TRAVEL_DATA_NOT_READY
