from logging import DEBUG, WARNING, FileHandler, StreamHandler, basicConfig, getLogger
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
