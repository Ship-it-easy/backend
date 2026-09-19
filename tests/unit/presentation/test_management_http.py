from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from auth.entrypoint.config import Config, PostgresConfig, SessionConfig
from auth.entrypoint.ioc.registry import get_providers
from auth.entrypoint.setup import create_async_ioc_container
from planning.application.errors import ConflictError
from planning.application.interactors.admin.projects import UpdateAdminProjectInteractor
from planning.application.interactors.admin.users import (
    BlockUserInteractor,
    CreateOwnerInteractor,
    CreateProjectUserInteractor,
)
from planning.application.interactors.project.engineer_access import (
    CreateEngineerAccessInteractor,
)
from planning.application.interactors.project.jobs import UpdateProjectJobInteractor
from planning.application.interactors.project.planning import (
    PublishPlanningRunInteractor,
)
from planning.entrypoint.config import PlanningServiceConfig
from planning.presentation.http.admin.router import admin_router
from planning.presentation.http.base.error_handler import (
    init_planning_error_handlers,
    planning_error_response,
)
from planning.presentation.http.engineer.router import engineer_router
from planning.presentation.http.project.router import project_router

EXPECTED_ROUTES = {
    ("GET", "/api/admin/owners"),
    ("POST", "/api/admin/owners"),
    ("GET", "/api/admin/projects"),
    ("POST", "/api/admin/projects"),
    ("GET", "/api/admin/projects/{project_id}"),
    ("PATCH", "/api/admin/projects/{project_id}"),
    ("POST", "/api/admin/projects/{project_id}/block"),
    ("POST", "/api/admin/projects/{project_id}/unblock"),
    ("GET", "/api/admin/projects/{project_id}/users"),
    ("POST", "/api/admin/projects/{project_id}/users"),
    ("POST", "/api/admin/users/{user_id}/reset-password"),
    ("POST", "/api/admin/users/{user_id}/block"),
    ("POST", "/api/admin/users/{user_id}/unblock"),
    ("GET", "/api/project/engineers"),
    ("POST", "/api/project/engineers"),
    ("GET", "/api/project/engineers/{engineer_id}"),
    ("PATCH", "/api/project/engineers/{engineer_id}"),
    ("PATCH", "/api/project/engineers/{engineer_id}/availability"),
    ("PUT", "/api/project/engineers/{engineer_id}/schedule"),
    ("POST", "/api/project/engineers/{engineer_id}/access"),
    ("POST", "/api/project/engineers/{engineer_id}/reset-password"),
    ("POST", "/api/project/engineers/{engineer_id}/access/block"),
    ("POST", "/api/project/engineers/{engineer_id}/access/unblock"),
    ("GET", "/api/project/qualifications"),
    ("POST", "/api/project/qualifications"),
    ("PATCH", "/api/project/qualifications/{item_id}"),
    ("GET", "/api/project/equipment-types"),
    ("POST", "/api/project/equipment-types"),
    ("PATCH", "/api/project/equipment-types/{item_id}"),
    ("POST", "/api/project/equipment-types/{item_id}/clear-quantity"),
    ("GET", "/api/project/work-types"),
    ("POST", "/api/project/work-types"),
    ("PATCH", "/api/project/work-types/{item_id}"),
    ("GET", "/api/project/jobs"),
    ("POST", "/api/project/jobs"),
    ("POST", "/api/project/jobs/import/preview"),
    ("POST", "/api/project/jobs/import/apply"),
    ("GET", "/api/project/jobs/{job_id}"),
    ("PATCH", "/api/project/jobs/{job_id}"),
    ("POST", "/api/project/jobs/{job_id}/status"),
    ("POST", "/api/project/jobs/{job_id}/cancel"),
    ("GET", "/api/project/address-suggestions"),
    ("GET", "/api/project/planning-config"),
    ("PATCH", "/api/project/planning-config"),
    ("GET", "/api/project/planning/readiness"),
    ("POST", "/api/project/planning/events/manual"),
    ("GET", "/api/project/planning/events/{event_id}"),
    ("GET", "/api/project/planning/current"),
    ("GET", "/api/project/planning/versions"),
    ("GET", "/api/project/planning/versions/{version_id}"),
    ("POST", "/api/project/planning/runs/{run_id}/publish"),
    ("GET", "/api/project/daily-plans/{planning_date}"),
    ("GET", "/api/engineer/assignments"),
    ("GET", "/api/engineer/assignments/route-state"),
    ("GET", "/api/engineer/assignments/{assignment_id}"),
    ("POST", "/api/engineer/assignments/{assignment_id}/start"),
    ("POST", "/api/engineer/assignments/{assignment_id}/complete"),
    ("POST", "/api/engineer/assignments/{assignment_id}/return-to-new"),
}


def management_app() -> FastAPI:
    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(project_router)
    app.include_router(engineer_router)
    init_planning_error_handlers(app)
    return app


def test_management_paths_and_methods_are_preserved() -> None:
    paths = management_app().openapi()["paths"]
    actual = {
        (method.upper(), path)
        for path, operations in paths.items()
        for method in operations
    }
    assert EXPECTED_ROUTES == actual
    assert len(actual) == len(set(actual))


def test_admin_request_validation_happens_before_interactor() -> None:
    with TestClient(management_app()) as client:
        response = client.post("/api/admin/owners", json={})
    assert response.status_code == 422
    assert {item["loc"][-1] for item in response.json()["detail"]} == {
        "login",
        "password",
    }


def test_project_request_validation_happens_before_interactor() -> None:
    with TestClient(management_app()) as client:
        response = client.post(
            "/api/project/jobs",
            json={"address": "", "sla_date": "bad", "work_type_id": 1},
        )
    assert response.status_code == 422


def test_engineer_query_validation_happens_before_interactor() -> None:
    with TestClient(management_app()) as client:
        response = client.get("/api/engineer/assignments?scope=invalid")
    assert response.status_code == 422


def test_application_conflict_has_stable_http_shape() -> None:
    response = planning_error_response(
        ConflictError(
            "The last active owner cannot be blocked",
            code="LAST_ACTIVE_OWNER",
        )
    )
    assert response.status_code == 409
    assert response.body == (
        b'{"code":"LAST_ACTIVE_OWNER",'
        b'"message":"The last active owner cannot be blocked"}'
    )


async def test_dishka_resolves_changed_management_interactors() -> None:
    postgres = PostgresConfig(
        host="localhost",
        port=5432,
        db="unused",
        user="unused",
        password="unused",
        uri="postgresql+psycopg://unused:unused@localhost:5432/unused",
    )
    planning = PlanningServiceConfig(
        valhalla_url="http://localhost",
        nominatim_url="http://localhost",
        nominatim_viewbox="",
        geoservice_timeout_sec=1,
        matrix_block_size=1,
    )
    container = create_async_ioc_container(
        get_providers(), Config(postgres, SessionConfig(5), planning)
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 1),
            "scheme": "http",
            "root_path": "",
        }
    )
    changed = (
        UpdateAdminProjectInteractor,
        BlockUserInteractor,
        CreateOwnerInteractor,
        CreateProjectUserInteractor,
        CreateEngineerAccessInteractor,
        UpdateProjectJobInteractor,
        PublishPlanningRunInteractor,
    )
    async with container({Request: request}) as scoped:
        for interactor_type in changed:
            assert isinstance(await scoped.get(interactor_type), interactor_type)
    await container.close()
