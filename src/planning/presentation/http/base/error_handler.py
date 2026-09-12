from typing import Any

from fastapi.responses import JSONResponse

from planning.application.errors import PlanningError


def planning_error_response(error: PlanningError) -> JSONResponse:
    content: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.run_id is not None:
        content["planning_run_id"] = error.run_id
    return JSONResponse(content=content, status_code=error.http_status)
