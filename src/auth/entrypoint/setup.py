import json
from datetime import datetime, timezone
from logging import (
    DEBUG,
    WARNING,
    FileHandler,
    Formatter,
    StreamHandler,
    basicConfig,
    getLogger,
)
from typing import Iterable

from dishka import AsyncContainer, Provider, make_async_container
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from auth.entrypoint.config import Config
from auth.presentation.http.base.error_handler import init_error_handlers
from auth.presentation.http.middlewares.asgi_auth import ASGIAuthMiddleware
from planning.presentation.http.base.error_handler import (
    init_planning_error_handlers,
)


def create_app(lifespan) -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    return app


def create_async_ioc_container(
    providers: Iterable[Provider], config: Config
) -> AsyncContainer:
    return make_async_container(*providers, context={Config: config})


def configure_app(app: FastAPI, root_router: APIRouter) -> None:
    app.include_router(root_router)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
        ],
        # Vite selects the next free local port when its default is occupied.
        # The batch endpoint uses Idempotency-Key, so browsers preflight it.
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(ASGIAuthMiddleware)
    init_error_handlers(app)
    init_planning_error_handlers(app)


_PLANNING_AUDIT_EVENTS = {
    "baseline_calculation_started",
    "baseline_assignment_completed",
    "baseline_distance_calculated",
    "baseline_validation_completed",
    "baseline_calculation_ready",
    "baseline_calculation_failed",
    "baseline_persistence_failed",
    "baseline_failure_marker_persistence_failed",
    "baseline_retry_scheduled",
    "planning_comparison_viewed",
    "planning_comparison_engineers_expanded",
}
_PLANNING_AUDIT_FIELDS = (
    "project_id",
    "planning_run_id",
    "planning_date",
    "plan_version",
    "algorithm_version",
    "input_hash",
    "duration_ms",
    "jobs_count",
    "engineers_count",
    "status",
    "attempt_count",
    "failure_code",
    "error_type",
    "coverage_comparable",
    "result_hash",
    "delay_seconds",
)


class PlanningAuditFormatter(Formatter):
    """Keep ordinary logs readable, emit audit events as safe JSON records."""

    def format(self, record):
        event = record.getMessage()
        if event not in _PLANNING_AUDIT_EVENTS:
            return super().format(record)
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "event": event,
            **{field: getattr(record, field, None) for field in _PLANNING_AUDIT_FIELDS},
        }
        if record.exc_info:
            error = record.exc_info[1]
            origin = getattr(error, "orig", error)
            diagnostics = getattr(origin, "diag", None)
            payload["exception_type"] = type(error).__name__
            payload["sqlstate"] = getattr(origin, "sqlstate", None)
            payload["constraint_name"] = getattr(diagnostics, "constraint_name", None)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level=DEBUG):
    format = (
        "[%(asctime)s.%(msecs)03d] %(module)15s:%(lineno)-3d "
        "%(levelname)-7s - %(message)s"
    )
    datefmt = "%Y-%m-%d %H:%M:%S"

    file_handler = FileHandler("logs.log")
    file_handler.setLevel(level)

    stream_handler = StreamHandler()
    stream_handler.setLevel(level)

    audit_formatter = PlanningAuditFormatter(format, datefmt=datefmt)
    file_handler.setFormatter(audit_formatter)
    stream_handler.setFormatter(audit_formatter)

    basicConfig(
        level=level,
        datefmt=datefmt,
        format=format,
        handlers=[file_handler, stream_handler],
    )
    # HTTP clients include the full request URL in their access logs. External
    # APIs commonly keep credentials in query parameters, so never emit them.
    getLogger("httpx").setLevel(WARNING)
    getLogger("httpcore").setLevel(WARNING)
