from datetime import date

from pydantic import BaseModel, ConfigDict


class StartPlanningBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requested_start_date: date


class StartPlanningBatchResponse(BaseModel):
    planning_event_id: int
    planning_batch_id: int | None = None
    status: str
    status_url: str
    reuse: bool = False
