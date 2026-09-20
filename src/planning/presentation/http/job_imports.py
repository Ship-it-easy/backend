from uuid import UUID

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, File, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel

from planning.application.interfaces.job_imports import JobImportOperations

router = APIRouter()


class ApplyBody(BaseModel):
    acknowledge_warnings: bool = False


@router.post("/{project_id}/job-imports", status_code=202)
@inject
async def upload(
    project_id: int,
    service: FromDishka[JobImportOperations],
    file: UploadFile = File(...),
):
    await service.ensure_access(project_id, write=True)
    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        raise HTTPException(
            415, detail={"code": "INVALID_FILE_TYPE", "message": "Требуется файл CSV"}
        )
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(
            413, detail={"code": "FILE_TOO_LARGE", "message": "Файл больше 10 МБ"}
        )
    return await service.upload(project_id, filename, content)


@router.get("/{project_id}/job-imports")
@inject
async def history(
    project_id: int,
    service: FromDishka[JobImportOperations],
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    return await service.list(project_id, limit, offset)


@router.get("/{project_id}/job-imports/{batch_id}")
@inject
async def get(project_id: int, batch_id: int, service: FromDishka[JobImportOperations]):
    return await service.get(project_id, batch_id)


@router.get("/{project_id}/job-imports/{batch_id}/issues")
@inject
async def issues(
    project_id: int,
    batch_id: int,
    service: FromDishka[JobImportOperations],
    severity: str | None = None,
    query: str = "",
    page: int = Query(1, ge=1),
):
    return await service.issues(project_id, batch_id, severity, query, page)


@router.post("/{project_id}/job-imports/{batch_id}/revalidate", status_code=202)
@inject
async def revalidate(
    project_id: int, batch_id: int, service: FromDishka[JobImportOperations]
):
    return await service.revalidate(project_id, batch_id)


@router.post("/{project_id}/job-imports/{batch_id}/apply")
@inject
async def apply(
    project_id: int,
    batch_id: int,
    body: ApplyBody,
    service: FromDishka[JobImportOperations],
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
):
    return await service.apply(
        project_id, batch_id, body.acknowledge_warnings, str(idempotency_key)
    )
