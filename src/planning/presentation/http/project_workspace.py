"""Explicitly project-scoped dispatcher workspace endpoints.

The legacy ``/api/project`` routes remain available for single-project clients.
All new UI traffic uses this router so the selected district is visible in the
request and authorization never depends on mutable session state.
"""

from datetime import date
from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Query

from planning.application.access import ProjectAccess
from planning.application.interactors.project.address_search import (
    SearchAddressesInteractor,
)
from planning.application.interactors.project.catalogs import (
    ClearEquipmentQuantityInteractor,
    CreateEquipmentTypeInteractor,
    CreateQualificationInteractor,
    CreateWorkTypeInteractor,
    ListEquipmentTypesInteractor,
    ListQualificationsInteractor,
    ListWorkTypesInteractor,
    UpdateEquipmentTypeInteractor,
    UpdateQualificationInteractor,
    UpdateWorkTypeInteractor,
)
from planning.application.interactors.project.engineer_access import (
    BlockEngineerAccessInteractor,
    CreateEngineerAccessInteractor,
    ResetEngineerPasswordInteractor,
    UnblockEngineerAccessInteractor,
)
from planning.application.interactors.project.engineers import (
    CreateEngineerInteractor,
    GetEngineerInteractor,
    ListEngineersInteractor,
    ReplaceEngineerScheduleInteractor,
    UpdateEngineerInteractor,
)
from planning.application.interactors.project.jobs import (
    CancelProjectJobInteractor,
    ChangeProjectJobStatusInteractor,
    CreateProjectJobInteractor,
    GetProjectJobInteractor,
    ListProjectJobsInteractor,
    UpdateProjectJobInteractor,
)
from planning.application.interfaces.traffic_route_service import TrafficRouteService
from planning.presentation.http.project.schemas import (
    AccessCreate,
    AccessPasswordReset,
    EngineerCreate,
    EngineerPatch,
    EquipmentCreate,
    EquipmentPatch,
    JobCreate,
    JobPatch,
    JobStatusChange,
    NamedCreate,
    NamedPatch,
    SchedulePut,
    WorkTypeCreate,
    WorkTypePatch,
)
from planning.presentation.http.project.traffic import (
    TrafficRouteRequest,
    build_traffic_route_response,
)

router = APIRouter(prefix="/{project_id}/workspace")


@router.post("/traffic/route")
@inject
async def scoped_traffic_route(
    project_id: int,
    body: TrafficRouteRequest,
    access: FromDishka[ProjectAccess],
    routes: FromDishka[TrafficRouteService],
) -> dict:
    return await build_project_traffic_route(project_id, body, access, routes)


async def build_project_traffic_route(
    project_id: int,
    body: TrafficRouteRequest,
    access: ProjectAccess,
    routes: TrafficRouteService,
) -> dict:
    await access.project(project_id)
    return await build_traffic_route_response(body, routes)


@router.get("/qualifications")
@inject
async def qualification_list(
    project_id: int, interactor: FromDishka[ListQualificationsInteractor]
) -> list[dict[str, Any]]:
    return await interactor(project_id)


@router.post("/qualifications", status_code=201)
@inject
async def qualification_create(
    project_id: int,
    body: NamedCreate,
    interactor: FromDishka[CreateQualificationInteractor],
) -> dict[str, Any]:
    return await interactor(body.model_dump(), project_id)


@router.patch("/qualifications/{item_id}")
@inject
async def qualification_patch(
    project_id: int,
    item_id: int,
    body: NamedPatch,
    interactor: FromDishka[UpdateQualificationInteractor],
) -> dict[str, Any]:
    return await interactor(
        item_id,
        body.model_dump(exclude_unset=True, exclude_none=True),
        project_id,
    )


@router.get("/equipment-types")
@inject
async def equipment_list(
    project_id: int, interactor: FromDishka[ListEquipmentTypesInteractor]
) -> list[dict[str, Any]]:
    return await interactor(project_id)


@router.post("/equipment-types", status_code=201)
@inject
async def equipment_create(
    project_id: int,
    body: EquipmentCreate,
    interactor: FromDishka[CreateEquipmentTypeInteractor],
) -> dict[str, Any]:
    return await interactor(body.model_dump(), project_id)


@router.patch("/equipment-types/{item_id}")
@inject
async def equipment_patch(
    project_id: int,
    item_id: int,
    body: EquipmentPatch,
    interactor: FromDishka[UpdateEquipmentTypeInteractor],
) -> dict[str, Any]:
    return await interactor(
        item_id,
        body.model_dump(exclude_unset=True, exclude_none=True),
        project_id,
    )


@router.post("/equipment-types/{item_id}/clear-quantity")
@inject
async def equipment_clear(
    project_id: int,
    item_id: int,
    interactor: FromDishka[ClearEquipmentQuantityInteractor],
) -> dict[str, Any]:
    return await interactor(item_id, project_id)


@router.get("/work-types")
@inject
async def work_type_list(
    project_id: int, interactor: FromDishka[ListWorkTypesInteractor]
) -> list[dict[str, Any]]:
    return await interactor(project_id)


@router.post("/work-types", status_code=201)
@inject
async def work_type_create(
    project_id: int,
    body: WorkTypeCreate,
    interactor: FromDishka[CreateWorkTypeInteractor],
) -> dict[str, Any]:
    values = body.model_dump(exclude={"qualification_ids", "equipment_type_ids"})
    return await interactor(
        values, body.qualification_ids, body.equipment_type_ids, project_id
    )


@router.patch("/work-types/{item_id}")
@inject
async def work_type_patch(
    project_id: int,
    item_id: int,
    body: WorkTypePatch,
    interactor: FromDishka[UpdateWorkTypeInteractor],
) -> dict[str, Any]:
    values = body.update_values()
    return await interactor(
        item_id,
        values,
        body.qualification_ids,
        body.equipment_type_ids,
        project_id,
    )


@router.get("/engineers")
@inject
async def engineer_list(
    project_id: int, interactor: FromDishka[ListEngineersInteractor]
) -> list[dict[str, Any]]:
    return await interactor(project_id)


@router.post("/engineers", status_code=201)
@inject
async def engineer_create(
    project_id: int,
    body: EngineerCreate,
    interactor: FromDishka[CreateEngineerInteractor],
) -> dict[str, Any]:
    values = body.model_dump(exclude={"qualification_ids"})
    return await interactor(values, body.qualification_ids, project_id)


@router.get("/engineers/{engineer_id}")
@inject
async def engineer_get(
    project_id: int,
    engineer_id: int,
    interactor: FromDishka[GetEngineerInteractor],
) -> dict[str, Any]:
    return await interactor(engineer_id, project_id)


@router.patch("/engineers/{engineer_id}")
@inject
async def engineer_patch(
    project_id: int,
    engineer_id: int,
    body: EngineerPatch,
    interactor: FromDishka[UpdateEngineerInteractor],
) -> dict[str, Any]:
    values = body.model_dump(exclude_none=True, exclude={"qualification_ids"})
    return await interactor(engineer_id, values, body.qualification_ids, project_id)


@router.put("/engineers/{engineer_id}/schedule")
@inject
async def schedule_put(
    project_id: int,
    engineer_id: int,
    body: SchedulePut,
    interactor: FromDishka[ReplaceEngineerScheduleInteractor],
) -> dict[str, Any]:
    return await interactor(
        engineer_id, [entry.model_dump() for entry in body.entries], project_id
    )


@router.patch("/engineers/{engineer_id}/availability")
@inject
async def availability_patch(
    project_id: int,
    engineer_id: int,
    body: SchedulePut,
    interactor: FromDishka[ReplaceEngineerScheduleInteractor],
) -> dict[str, Any]:
    return await interactor(
        engineer_id, [entry.model_dump() for entry in body.entries], project_id
    )


@router.post("/engineers/{engineer_id}/access", status_code=201)
@inject
async def engineer_access_create(
    project_id: int,
    engineer_id: int,
    body: AccessCreate,
    interactor: FromDishka[CreateEngineerAccessInteractor],
) -> dict[str, Any]:
    return await interactor(engineer_id, body.login, body.password, project_id)


@router.post("/engineers/{engineer_id}/reset-password")
@inject
async def engineer_password_reset(
    project_id: int,
    engineer_id: int,
    body: AccessPasswordReset,
    interactor: FromDishka[ResetEngineerPasswordInteractor],
) -> dict[str, str]:
    return await interactor(engineer_id, body.password, project_id)


@router.post("/engineers/{engineer_id}/access/block")
@inject
async def engineer_access_block(
    project_id: int,
    engineer_id: int,
    interactor: FromDishka[BlockEngineerAccessInteractor],
) -> dict[str, Any]:
    return await interactor(engineer_id, project_id)


@router.post("/engineers/{engineer_id}/access/unblock")
@inject
async def engineer_access_unblock(
    project_id: int,
    engineer_id: int,
    interactor: FromDishka[UnblockEngineerAccessInteractor],
) -> dict[str, Any]:
    return await interactor(engineer_id, project_id)


@router.get("/jobs")
@inject
async def job_list(
    project_id: int,
    interactor: FromDishka[ListProjectJobsInteractor],
    search: str | None = None,
    job_status: str | None = Query(default=None, alias="status"),
    sla_date: date | None = None,
    work_type_id: int | None = None,
    assigned: bool | None = None,
    import_batch_id: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    return await interactor(
        search=search,
        status=job_status,
        sla_date=sla_date,
        work_type_id=work_type_id,
        assigned=assigned,
        import_batch_id=import_batch_id,
        limit=limit,
        offset=offset,
        scoped_project_id=project_id,
    )


@router.post("/jobs", status_code=201)
@inject
async def job_create(
    project_id: int,
    body: JobCreate,
    interactor: FromDishka[CreateProjectJobInteractor],
) -> dict[str, Any]:
    return await interactor(body.model_dump(), project_id)


@router.get("/jobs/{job_id}")
@inject
async def job_get(
    project_id: int,
    job_id: int,
    interactor: FromDishka[GetProjectJobInteractor],
) -> dict[str, Any]:
    return await interactor(job_id, project_id)


@router.patch("/jobs/{job_id}")
@inject
async def job_patch(
    project_id: int,
    job_id: int,
    body: JobPatch,
    interactor: FromDishka[UpdateProjectJobInteractor],
) -> dict[str, Any]:
    return await interactor(
        job_id, body.model_dump(exclude_unset=True), project_id
    )


@router.post("/jobs/{job_id}/status")
@inject
async def job_status_change(
    project_id: int,
    job_id: int,
    body: JobStatusChange,
    interactor: FromDishka[ChangeProjectJobStatusInteractor],
    cancel_interactor: FromDishka[CancelProjectJobInteractor],
) -> dict[str, Any]:
    if body.status == "CANCELLED":
        return await cancel_interactor(job_id, project_id)
    return await interactor(job_id, body.status, body.reason, project_id)


@router.post("/jobs/{job_id}/cancel", status_code=202)
@inject
async def job_cancel(
    project_id: int,
    job_id: int,
    interactor: FromDishka[CancelProjectJobInteractor],
) -> dict[str, Any]:
    return await interactor(job_id, project_id)


@router.get("/address-suggestions")
@inject
async def address_suggestions(
    project_id: int,
    interactor: FromDishka[SearchAddressesInteractor],
    q: str = Query(min_length=3, max_length=300),
) -> list[dict[str, Any]]:
    return await interactor(q, project_id)
