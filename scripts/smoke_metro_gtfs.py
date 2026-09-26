"""Check daytime GTFS metro and avoid overnight waits without API fallback."""

import argparse
import asyncio
import json
from datetime import date, datetime, time

from planning.domain.traffic import MOSCOW
from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.traffic_route import build_traffic_route
from planning.presentation.http.project.traffic import TrafficRouteRequest


async def main(day: date) -> None:
    config = PlanningServiceConfig.from_env()
    config.mosmetro_url = ""
    config.geoservice_timeout_sec = 60
    rows = []
    for clock in (time(8, 30), time(14, 30), time(23, 30)):
        request = TrafficRouteRequest(
            departure_at=datetime.combine(day, clock, tzinfo=MOSCOW),
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
                "date": day.isoformat(),
                "departure": clock.strftime("%H:%M"),
                "duration_seconds": result["duration_seconds"],
                "transit": [
                    {"type": segment.get("travel_type"), "route": segment["road"]}
                    for segment in transit
                ],
            }
        )
        if clock != time(23, 30) and not any(
            segment.get("travel_type") in {"subway", "metro"}
            for segment in transit
        ):
            raise SystemExit(f"GTFS metro was not selected at {clock:%H:%M}")
        if clock == time(23, 30) and result["duration_seconds"] > 4 * 3600:
            raise SystemExit("Late route waits too long for the next metro service")
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=datetime.now(MOSCOW).date(),
        help="Moscow service date in YYYY-MM-DD format (default: today)",
    )
    asyncio.run(main(parser.parse_args().date))
