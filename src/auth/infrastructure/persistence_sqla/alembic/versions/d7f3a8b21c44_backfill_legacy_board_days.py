"""backfill legacy planning board days across published versions

Revision ID: d7f3a8b21c44
Revises: c1a7e9d42f60
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d7f3a8b21c44"
down_revision: Union[str, None] = "c1a7e9d42f60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Dynamic publications can preserve assignments from an earlier version.
    # Link each target version/date to the most recent compatible successful
    # source run. A route result is not copied after one of its assigned jobs
    # moved to another date/engineer or disappeared from the target version.
    op.execute("""
        INSERT INTO planning_day_results (
            plan_version_id, project_id, planning_date, planning_run_id,
            input_job_ids_hash, input_jobs_count, assigned_count,
            unassigned_count, solver_status
        )
        SELECT DISTINCT ON (target.id, run.planning_date)
            target.id,
            target.project_id,
            run.planning_date,
            run.id,
            run.normalized_input_hash,
            run.input_jobs_count,
            run.assigned_jobs_count,
            run.unassigned_jobs_count,
            run.solver_status
        FROM project_plan_versions target
        JOIN project_plan_versions source
          ON source.project_id = target.project_id
         AND source.version_number <= target.version_number
         AND source.planning_batch_id IS NOT NULL
        JOIN planning_runs run
          ON run.planning_batch_id = source.planning_batch_id
         AND run.status = 'SUCCESS'
        WHERE NOT EXISTS (
            SELECT 1
            FROM planning_route_jobs route_job
            JOIN planning_routes route
              ON route.id = route_job.planning_route_id
            LEFT JOIN project_plan_assignments assignment
              ON assignment.plan_version_id = target.id
             AND assignment.job_id = route_job.job_id
            WHERE route_job.planning_run_id = run.id
              AND (
                  assignment.id IS NULL
                  OR assignment.planning_date <> run.planning_date
                  OR assignment.engineer_id <> route.engineer_id
              )
        )
        ORDER BY
            target.id,
            run.planning_date,
            source.version_number DESC,
            run.id DESC
        ON CONFLICT (plan_version_id, planning_date) DO UPDATE SET
            project_id = EXCLUDED.project_id,
            planning_run_id = EXCLUDED.planning_run_id,
            input_job_ids_hash = EXCLUDED.input_job_ids_hash,
            input_jobs_count = EXCLUDED.input_jobs_count,
            assigned_count = EXCLUDED.assigned_count,
            unassigned_count = EXCLUDED.unassigned_count,
            solver_status = EXCLUDED.solver_status
    """)


def downgrade() -> None:
    # The rows are valid read-model data and may have been extended by normal
    # publications after the upgrade. Dropping them during a code rollback
    # would make that published information unavailable, so keep them intact.
    pass
