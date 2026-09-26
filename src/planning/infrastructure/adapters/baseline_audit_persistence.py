from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.services.baseline_fifo import BASELINE_TRAVEL_MINUTES
from planning.domain.entities.baseline import BaselinePlanResult
from planning.infrastructure.persistence_sqla.mappings.tables import (
    planning_baseline_route_jobs,
    planning_baseline_routes,
    planning_baseline_unassigned_jobs,
)


async def replace_baseline_audit_rows(
    session: AsyncSession,
    *,
    baseline_result_id: int,
    project_id: int,
    baseline: BaselinePlanResult,
) -> None:
    """Replace the queryable audit projection for one immutable baseline row."""
    await session.execute(
        delete(planning_baseline_route_jobs).where(
            planning_baseline_route_jobs.c.baseline_result_id == baseline_result_id
        )
    )
    await session.execute(
        delete(planning_baseline_unassigned_jobs).where(
            planning_baseline_unassigned_jobs.c.baseline_result_id
            == baseline_result_id
        )
    )
    await session.execute(
        delete(planning_baseline_routes).where(
            planning_baseline_routes.c.baseline_result_id == baseline_result_id
        )
    )
    if baseline.status != "READY":
        return

    for route in baseline.routes:
        route_id = await session.scalar(
            insert(planning_baseline_routes)
            .values(
                project_id=project_id,
                baseline_result_id=baseline_result_id,
                engineer_id=route.engineer_id,
                engineer_input_order=route.engineer_input_order,
                shift_start=route.shift_start,
                shift_end=route.shift_end,
                capacity_minutes=route.capacity_minutes,
                assigned_jobs_count=len(route.jobs),
                consumed_minutes=route.consumed_minutes,
                remaining_minutes=route.remaining_minutes,
                distance_meters=route.distance_meters,
                start_location_snapshot=route.start_location_snapshot,
            )
            .returning(planning_baseline_routes.c.id)
        )
        if route_id is None:
            raise RuntimeError("Failed to persist baseline route")
        if route.jobs:
            await session.execute(
                insert(planning_baseline_route_jobs),
                [
                    {
                        "project_id": project_id,
                        "baseline_result_id": baseline_result_id,
                        "baseline_route_id": int(route_id),
                        "job_id": item.job_id,
                        "job_input_order": item.job_input_order,
                        "route_position": item.route_position,
                        "received_at": item.received_at,
                        "ingest_sequence": item.ingest_sequence,
                        "service_duration_minutes": item.service_duration_minutes,
                        "standard_travel_minutes": BASELINE_TRAVEL_MINUTES,
                        "planned_start": item.planned_start,
                        "planned_finish": item.planned_finish,
                        "window_from_min": item.window_from_min,
                        "window_to_min": item.window_to_min,
                        "window_hit": item.window_hit,
                        "previous_location_type": item.previous_location_type,
                        "distance_from_previous_meters": (
                            item.distance_from_previous_meters
                        ),
                    }
                    for item in route.jobs
                ],
            )

    if baseline.unassigned:
        await session.execute(
            insert(planning_baseline_unassigned_jobs),
            [
                {
                    "project_id": project_id,
                    "baseline_result_id": baseline_result_id,
                    "job_id": item.job_id,
                    "job_input_order": item.job_input_order,
                    "reason_code": item.reason_code,
                    "diagnostics": item.diagnostics,
                }
                for item in baseline.unassigned
            ],
        )
