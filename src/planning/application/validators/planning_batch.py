from collections import Counter
from datetime import date
from typing import Iterable


class PlanningBatchValidator:
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
