from collections import Counter
from datetime import date
from typing import Iterable

from planning.domain.entities.planning import PlanningInput, PlanningResult


class PlanningBatchValidator:
    def validate_day(
        self,
        data: PlanningInput,
        result: PlanningResult,
        remaining_job_ids: set[int],
        previously_assigned_job_ids: Iterable[int],
        effective_start: date,
        maximum_end: date,
        decisions: dict[int, dict],
    ) -> list[str]:
        """Validate the prospective batch transition before it is committed."""
        errors: list[str] = []
        assigned = [item.job_id for route in result.routes for item in route.jobs]
        assigned_set = set(assigned)
        previous = set(previously_assigned_job_ids)
        if not effective_start <= data.planning_date <= maximum_end:
            errors.append("planning day is outside batch horizon")
        if assigned_set - remaining_job_ids:
            errors.append(
                "day assigns jobs that are not in the remaining set: "
                f"{sorted(assigned_set - remaining_job_ids)}"
            )
        if assigned_set & previous:
            errors.append(
                "day reassigns jobs already assigned on an earlier day: "
                f"{sorted(assigned_set & previous)}"
            )
        candidate_ids = {item.id for item in data.jobs}
        if candidate_ids - remaining_job_ids:
            errors.append("solver input contains jobs outside the remaining set")
        missing_decisions = candidate_ids - decisions.keys()
        if missing_decisions:
            errors.append(
                f"priority decisions are missing for jobs: {sorted(missing_decisions)}"
            )
        for job_id in candidate_ids & decisions.keys():
            decision = decisions[job_id]
            if int(decision.get("cascade_drop_penalty", -1)) < 0:
                errors.append(f"job {job_id} has an invalid cascade penalty")
            if int(decision.get("future_opportunity_count", -1)) < 0:
                errors.append(f"job {job_id} has an invalid opportunity count")
        return errors

    def validate(
        self,
        assigned_job_ids: Iterable[int],
        effective_start: date,
        maximum_end: date,
        assignment_dates: dict[int, date],
    ) -> list[str]:
        assigned = list(assigned_job_ids)
        errors = [
            f"job {job_id} is assigned more than once"
            for job_id, count in Counter(assigned).items()
            if count > 1
        ]
        for job_id, planning_date in assignment_dates.items():
            if not effective_start <= planning_date <= maximum_end:
                errors.append(f"job {job_id} is assigned outside batch horizon")
        return errors
