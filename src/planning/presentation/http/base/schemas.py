from pydantic import BaseModel


class ErrorResponse(BaseModel):
    code: str
    message: str
    planning_run_id: int | None = None


ERROR_RESPONSES = {
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}
