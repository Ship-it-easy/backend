from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str | None = Field(default=None, max_length=255)
    address: str = Field(min_length=1)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    sla_date: date
    work_type_id: int
    service_duration_min: int | None = Field(default=None, gt=0)
    time_window_start: time | None = None
    time_window_end: time | None = None

    @model_validator(mode="after")
    def validate_time_window(self) -> "CreateJobRequest":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("coordinates must be provided together")
        if self.time_window_start is not None and self.time_window_end is not None:
            if self.time_window_start >= self.time_window_end:
                raise ValueError(
                    "time_window_start must be earlier than time_window_end"
                )
        return self


class JobResponse(BaseModel):
    id: int
    project_id: int
    external_id: str | None
    status: str
    priority: str
    address: str
    latitude: float | None
    longitude: float | None
    sla_date: date
    time_window_start: time | None
    time_window_end: time | None
    work_type_id: int
    service_duration_min: int | None
    created_at: datetime
    updated_at: datetime
    planning_event_id: int | None = None
