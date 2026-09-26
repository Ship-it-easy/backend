"""Compare walking, Valhalla multimodal, and optional metro API for one pair."""

import argparse
import asyncio
import json
from datetime import datetime

import httpx

from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.mosmetro import (
    MosMetroClient,
    build_metro_map_candidate,
)
from planning.infrastructure.adapters.traffic_route import evaluate_trip


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("latitude_from", type=float)
    parser.add_argument("longitude_from", type=float)
    parser.add_argument("latitude_to", type=float)
    parser.add_argument("longitude_to", type=float)
    parser.add_argument("departure_at", type=datetime.fromisoformat)
    args = parser.parse_args()
    if args.departure_at.tzinfo is None:
        parser.error("departure_at must include a timezone offset")
    config = PlanningServiceConfig.from_env()
    origin = args.latitude_from, args.longitude_from
    destination = args.latitude_to, args.longitude_to
    result = {}
    async with httpx.AsyncClient(
        base_url=config.valhalla_url, timeout=max(30, config.geoservice_timeout_sec)
    ) as client:
        for profile in ("pedestrian", "multimodal"):
            response = await client.post(
                "/route",
                json={
                    "locations": [
                        {"lat": origin[0], "lon": origin[1]},
                        {"lat": destination[0], "lon": destination[1]},
                    ],
                    "costing": profile,
                    "date_time": {
                        "type": 1,
                        "value": args.departure_at.strftime("%Y-%m-%dT%H:%M"),
                    },
                    "shape_format": "polyline6",
                },
            )
            response.raise_for_status()
            trip = evaluate_trip(response.json()["trip"], args.departure_at, profile)
            result[profile] = {
                "seconds": round(trip["duration_seconds"]),
                "modes": sorted(
                    {
                        segment.get("travel_type") or segment.get("travel_mode")
                        for segment in trip["segments"]
                    }
                ),
            }
        metro = MosMetroClient(config.mosmetro_url, config.geoservice_timeout_sec)
        if metro.enabled:
            candidate = await build_metro_map_candidate(
                metro,
                client,
                origin,
                destination,
                args.departure_at,
                evaluate_trip,
                config.mosmetro_max_access_meters,
                config.mosmetro_waiting_seconds,
            )
            result["metro_api_fallback"] = (
                {"seconds": round(candidate["duration_seconds"])}
                if candidate else None
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
