"""Exercise a real cross-region route with stops and departure alternatives."""

import asyncio
import json
from pathlib import Path

from planning.domain.traffic import area_profile
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.traffic_route import build_traffic_route
from planning.presentation.http.project.traffic import TrafficRouteRequest

ROOT = Path(__file__).resolve().parents[1]


async def main():
    audit = json.loads(
        (ROOT / "data/traffic/coverage_audit.json").read_text(encoding="utf-8")
    )
    entries = [r for r in audit["addresses"] if r["status"] == "building_match"]
    known = {r["address"]: r for r in entries}
    office = next(
        known[o["address"]] for o in audit["offices"] if o["address"] in known
    )
    stops = [office] + [
        next(r for r in entries if area_profile(r["latitude"], r["longitude"]) == zone)
        for zone in ("domodedovo", "stupino", "kashira_local")
    ]
    request = TrafficRouteRequest(
        departure_at="2026-08-17T08:00:00+03:00",
        stops=[
            {
                "latitude": s["latitude"],
                "longitude": s["longitude"],
                "job_id": i if i else None,
                "service_seconds": 1800 if i else 0,
                "access_seconds": 300 if i else 0,
            }
            for i, s in enumerate(stops)
        ],
    )
    result = await build_traffic_route(request, PlanningServiceConfig.from_env())
    assert result["coverage_status"] == "complete"
    assert len(result["departure_options"]) == 13
    assert len(result["legs"]) == 3
    assert (
        abs(
            result["elapsed_seconds"]
            - sum(
                result[k]
                for k in (
                    "duration_seconds",
                    "waiting_seconds",
                    "service_seconds",
                    "access_seconds",
                )
            )
        )
        <= 1
    )
    report = {
        "case": "hypothetical_day_at_real_source_addresses",
        "service_seconds_assumption": 1800,
        "access_seconds_assumption": 300,
        "stops": stops,
        "result": result,
    }
    (ROOT / "data/traffic/smoke_day.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "duration_seconds",
                    "finish_at",
                    "coverage_status",
                    "elapsed_seconds",
                )
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
