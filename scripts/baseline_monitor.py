"""Read-only FIFO monitoring snapshot for DEV/PROD scheduled health checks.

Exit status 2 means an alert condition; stdout remains machine-readable JSON.
Only aggregate counts and hashes are loaded, never addresses or names.
"""

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from math import ceil

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from auth.entrypoint.config import PostgresConfig
from planning.infrastructure.persistence_sqla.mappings.tables import (
    planning_baseline_results,
    planning_plan_comparisons,
    planning_runs,
    project_plan_versions,
)

MATRIX_FAILURES = {
    "BASELINE_DISTANCE_DATA_NOT_READY",
    "BASELINE_TRAVEL_PROVIDER_UNAVAILABLE",
}
ROUTE_UNAVAILABLE = "BASELINE_ROUTE_UNAVAILABLE"


def percentile(values: list[int | float], rank: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, ceil(rank * len(ordered)) - 1)
    return float(ordered[index])


def summarize(rows: list[dict], previous_rows: list[dict]) -> dict:
    statuses = Counter(str(row["status"]) for row in rows)
    previous = Counter(str(row["status"]) for row in previous_rows)
    total = len(rows)
    eligible = [
        row for row in rows
        if row["status"] in {"READY", "FAILED"}
        and row["failure_code"] != ROUTE_UNAVAILABLE
    ]
    durations = []
    for row in rows:
        if row["status"] not in {"READY", "FAILED"}:
            continue
        if row.get("started_at") is not None and row.get("finished_at") is not None:
            durations.append(
                max(
                    0,
                    int(
                        (row["finished_at"] - row["started_at"]).total_seconds() * 1000
                    ),
                )
            )
        elif row["calculation_time_ms"] is not None:
            durations.append(int(row["calculation_time_ms"]))
    jobs = [int(row["input_jobs_count"] or 0) for row in rows]
    engineers = [int(row["engineers_count"] or 0) for row in rows]
    comparable = [
        bool(row["coverage_comparable"])
        for row in rows
        if row["status"] == "READY" and row["coverage_comparable"] is not None
    ]
    delays = [
        max(
            0,
            int((row["finished_at"] - row["published_at"]).total_seconds() * 1000),
        )
        for row in rows
        if row["status"] == "READY"
        and row["finished_at"] is not None
        and row["published_at"] is not None
    ]
    hashes: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in rows:
        if row["status"] == "READY" and row["result_hash"]:
            hashes[
                (
                    row["input_hash"],
                    row["algorithm_version"],
                    row["travel_matrix_hash"],
                )
            ].add(row["result_hash"])
    divergence = sum(len(values) > 1 for values in hashes.values())
    failed_rate = statuses["FAILED"] / total if total else 0
    previous_failed_rate = (
        previous["FAILED"] / len(previous_rows) if previous_rows else 0
    )
    hash_mismatches = sum(
        row["failure_code"] == "BASELINE_INPUT_HASH_MISMATCH" for row in rows
    )
    alerts = []
    if statuses["FAILED"] >= 3 and failed_rate > previous_failed_rate + 0.05:
        alerts.append("BASELINE_FAILED_RATE_INCREASED")
    if hash_mismatches:
        alerts.append("BASELINE_INPUT_HASH_MISMATCH")
    if divergence:
        alerts.append("BASELINE_RESULT_HASH_DIVERGENCE")
    return {
        "total_results": total,
        "status_count": dict(statuses),
        "status_share": {
            status: (statuses[status] / total if total else 0)
            for status in ("READY", "FAILED", "NOT_APPLICABLE_SHIFT_STARTED")
        },
        "duration_ms": {
            "p50": percentile(durations, 0.50),
            "p95": percentile(durations, 0.95),
            "p99": percentile(durations, 0.99),
        },
        "input_size": {
            "jobs_max": max(jobs, default=0),
            "jobs_p95": percentile(jobs, 0.95),
            "engineers_max": max(engineers, default=0),
            "engineers_p95": percentile(engineers, 0.95),
        },
        "retry_count": sum(max(0, int(row["attempt_count"] or 0) - 1) for row in rows),
        "exhausted_retries": sum(
            row["status"] == "FAILED" and int(row["attempt_count"] or 0) >= 3
            for row in rows
        ),
        "matrix_errors": sum(row["failure_code"] in MATRIX_FAILURES for row in rows),
        "route_unavailable": sum(
            row["failure_code"] == ROUTE_UNAVAILABLE for row in rows
        ),
        "ready_share_route_eligible": (
            sum(row["status"] == "READY" for row in eligible) / len(eligible)
            if eligible else None
        ),
        "input_hash_mismatches": hash_mismatches,
        "coverage_mismatch_share": (
            sum(not value for value in comparable) / len(comparable)
            if comparable
            else None
        ),
        "result_hash_divergences": divergence,
        "publication_delay_ms": {
            "p50": percentile(delays, 0.50),
            "p95": percentile(delays, 0.95),
            "p99": percentile(delays, 0.99),
        },
        "alerts": alerts,
    }


async def collect(window_hours: int, project_id: int | None = None) -> dict:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)
    previous_since = since - timedelta(hours=window_hours)
    statement = (
        select(
            planning_baseline_results.c.project_id,
            planning_baseline_results.c.created_at,
            planning_baseline_results.c.status,
            planning_baseline_results.c.algorithm_version,
            planning_baseline_results.c.input_hash,
            planning_baseline_results.c.travel_matrix_hash,
            planning_baseline_results.c.result_hash,
            planning_baseline_results.c.input_jobs_count,
            planning_baseline_results.c.calculation_time_ms,
            planning_baseline_results.c.attempt_count,
            planning_baseline_results.c.failure_code,
            planning_baseline_results.c.started_at,
            planning_baseline_results.c.finished_at,
            planning_plan_comparisons.c.coverage_comparable,
            project_plan_versions.c.published_at,
            func.jsonb_array_length(planning_runs.c.input_snapshot["engineers"]).label(
                "engineers_count"
            ),
        )
        .join(
            planning_runs,
            planning_runs.c.id == planning_baseline_results.c.planning_run_id,
        )
        .outerjoin(
            planning_plan_comparisons,
            planning_plan_comparisons.c.baseline_result_id
            == planning_baseline_results.c.id,
        )
        .outerjoin(
            project_plan_versions,
            project_plan_versions.c.id == planning_baseline_results.c.plan_version_id,
        )
        .where(planning_baseline_results.c.created_at >= previous_since)
    )
    if project_id is not None:
        statement = statement.where(
            planning_baseline_results.c.project_id == project_id
        )
    engine = create_async_engine(PostgresConfig.from_env().uri)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(statement)
            rows = [dict(row) for row in result.mappings()]
    finally:
        await engine.dispose()
    current_rows = [row for row in rows if row["created_at"] >= since]
    previous_rows = [row for row in rows if row["created_at"] < since]
    return {
        "as_of": now.isoformat(),
        "window_hours": window_hours,
        "project_id": project_id,
        **summarize(current_rows, previous_rows),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--project-id", type=int)
    arguments = parser.parse_args()
    if arguments.window_hours < 1 or arguments.window_hours > 168:
        parser.error("--window-hours must be between 1 and 168")
    snapshot = asyncio.run(collect(arguments.window_hours, arguments.project_id))
    print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(2 if snapshot["alerts"] else 0)
