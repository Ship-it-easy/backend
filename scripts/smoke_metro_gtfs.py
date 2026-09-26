"""Show GTFS metro choices for several times without API fallback."""

import asyncio
import json
from datetime import datetime, time

from planning.domain.traffic import MOSCOW
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.traffic_route import build_traffic_route
from planning.presentation.http.project.traffic import TrafficRouteRequest


async def main() -> None:
    config = PlanningServiceConfig.from_env()
    config.mosmetro_url = ""
    config.geoservice_timeout_sec = 60
    today = datetime.now(MOSCOW).date()
    rows = []
    for clock in (time(8, 30), time(14, 30), time(23, 30)):
        request = TrafficRouteRequest(
            departure_at=datetime.combine(today, clock, tzinfo=MOSCOW),
            profile="multimodal",
            include_departure_options=False,
            stops=[
                {"latitude": 55.814789, "longitude": 37.733928},
                {"latitude": 55.710438, "longitude": 37.559317},
            ],
        )
        result = await build_traffic_route(request, config)
        transit = [
            segment
            for leg in result["legs"]
            for segment in leg["segments"]
            if segment.get("travel_mode") == "transit"
        ]
        rows.append(
            {
                "departure": clock.strftime("%H:%M"),
                "duration_seconds": result["duration_seconds"],
                "transit": [
                    {"type": segment.get("travel_type"), "route": segment["road"]}
                    for segment in transit
                ],
            }
        )
        if not any(
            segment.get("travel_type") in {"subway", "metro"}
            for segment in transit
        ):
            raise SystemExit(f"GTFS metro was not selected at {clock:%H:%M}")
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
