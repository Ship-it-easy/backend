from datetime import date, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NamedCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    active: bool = True


class NamedPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    active: bool | None = None


class EquipmentCreate(NamedCreate):
    available_units: int = Field(default=0, ge=0)


class EquipmentPatch(NamedPatch):
    available_units: int | None = Field(default=None, ge=0)


class WorkTypeCreate(NamedCreate):
    priority: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"] = "LOW"
    default_service_duration_min: int = Field(gt=0)
    required_transport: Literal["CAR"] | None = None
    qualification_ids: list[int] = Field(default_factory=list)
    equipment_type_ids: list[int] = Field(default_factory=list)


class WorkTypePatch(NamedPatch):
    priority: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"] | None = None
    default_service_duration_min: int | None = Field(default=None, gt=0)
    required_transport: Literal["CAR"] | None = None
    qualification_ids: list[int] | None = None
    equipment_type_ids: list[int] | None = None


class EngineerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    active: bool = True
    transport_type: Literal["CAR", "NONE"]
    start_address: str = Field(min_length=1)
    start_latitude: float | None = None
    start_longitude: float | None = None
    qualification_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def coordinate_pair(self) -> "EngineerCreate":
        if (self.start_latitude is None) != (self.start_longitude is None):
            raise ValueError("coordinates must be provided together")
        return self


class EngineerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    active: bool | None = None
    transport_type: Literal["CAR", "NONE"] | None = None
    start_address: str | None = Field(default=None, min_length=1)
    start_latitude: float | None = None
    start_longitude: float | None = None
    qualification_ids: list[int] | None = None


class ScheduleEntry(BaseModel):
    work_date: date
    working: bool = True
    shift_start: time | None = None
    shift_end: time | None = None

    @model_validator(mode="after")
    def valid_shift(self) -> "ScheduleEntry":
        if self.working and (
            self.shift_start is None
            or self.shift_end is None
            or self.shift_start >= self.shift_end
        ):
            raise ValueError("working date requires shift_start < shift_end")
        return self


class SchedulePut(BaseModel):
    entries: list[ScheduleEntry] = Field(min_length=1, max_length=366)


class AccessCreate(BaseModel):
    login: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class AccessPasswordReset(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str = Field(min_length=1, max_length=1000)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    sla_date: date
    time_window_start: time | None = None
    time_window_end: time | None = None
    work_type_id: int

    @model_validator(mode="after")
    def validate_values(self) -> "JobCreate":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("coordinates must be provided together")
        if (
            self.time_window_start
            and self.time_window_end
            and self.time_window_start >= self.time_window_end
        ):
            raise ValueError("time window cannot cross midnight")
        return self


class JobPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str | None = Field(default=None, min_length=1, max_length=1000)
    latitude: float | None = None
    longitude: float | None = None
    sla_date: date | None = None
    time_window_start: time | None = None
    time_window_end: time | None = None
    work_type_id: int | None = None


class JobStatusChange(BaseModel):
    status: Literal["NEW", "IN_PROGRESS", "COMPLETED", "CANCELLED"]
    reason: str | None = Field(default=None, max_length=1000)


class PublishRequest(BaseModel):
    confirm_unassigned: bool = False
    confirm_partial_batch: bool = False


class PlanningConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sla_overdue_base: int | None = Field(default=None, ge=0)
    sla_overdue_per_day: int | None = Field(default=None, ge=0)
    sla_today: int | None = Field(default=None, ge=0)
    sla_tomorrow: int | None = Field(default=None, ge=0)
    sla_2_3_days: int | None = Field(default=None, ge=0)
    sla_later: int | None = Field(default=None, ge=0)
    skill_one_engineer: int | None = Field(default=None, ge=0)
    skill_two_engineers: int | None = Field(default=None, ge=0)
    equipment_one_unit: int | None = Field(default=None, ge=0)
    equipment_two_units: int | None = Field(default=None, ge=0)
    window_30: int | None = Field(default=None, ge=0)
    window_60: int | None = Field(default=None, ge=0)
    window_120: int | None = Field(default=None, ge=0)
    travel_cost_per_minute: int | None = Field(default=None, ge=0)
    solver_time_limit_sec: int | None = Field(default=None, ge=1, le=600)
    max_jobs_per_run: int | None = Field(default=None, ge=1, le=1000)
    future_opportunity_critical: int | None = Field(default=None, ge=0, le=1_000_000)
    future_opportunity_high: int | None = Field(default=None, ge=0, le=1_000_000)
    future_opportunity_limited: int | None = Field(default=None, ge=0, le=1_000_000)
    batch_total_time_limit_sec: int | None = Field(default=None, ge=1, le=900)
    max_jobs_per_batch: int | None = Field(default=None, ge=1, le=5000)
    solver_seed: int | None = Field(default=None, ge=0, le=2_147_483_647)
    candidate_solver_time_limit_sec: int | None = Field(default=None, ge=1, le=600)
    single_cascade_time_limit_sec: int | None = Field(default=None, ge=1, le=3600)
    event_time_limit_sec: int | None = Field(default=None, ge=1, le=3600)
    event_coalesce_window_sec: int | None = Field(default=None, ge=0, le=120)
    event_coalesce_max_wait_sec: int | None = Field(default=None, ge=1, le=600)
    max_parallel_candidate_models: int | None = Field(default=None, ge=1, le=32)
    distance_unit_meters: int | None = Field(default=None, ge=1, le=100_000)
    time_unit_seconds: int | None = Field(default=None, ge=1, le=86_400)
    travel_cache_ttl_days: int | None = Field(default=None, ge=0, le=3_650)
    traffic_enabled: bool | None = None
    traffic_reliability_buffer: float | None = Field(default=None, ge=1.0, le=2.0)
    traffic_profile: Literal[
        "moscow_default",
        "yuvao_volgogradka_ryazanka",
        "south_kashirka_biryulevo",
        "yugocentr_profsoyuznaya_varshavka",
        "center_ttk_sadovoe",
        "oblast_domodedovo_kashira_stupino",
    ] | None = None
    nightly_planning_enabled: bool | None = None
    nightly_planning_time: time | None = None
    travel_provider: Literal["VALHALLA_LOCAL"] | None = None
