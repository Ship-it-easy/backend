"""Check the optional MosMetro API independently from GTFS routing."""

import asyncio
import json

from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.mosmetro import MosMetroClient


async def main() -> None:
    config = PlanningServiceConfig.from_env()
    if not config.mosmetro_url:
        raise SystemExit("MOSMETRO_URL is not configured")
    client = MosMetroClient(config.mosmetro_url, config.geoservice_timeout_sec)
    stations = await client.stations()
    journey = await client.route(1, 22)
    if journey is None:
        raise SystemExit("MosMetro API returned no route")
    print(json.dumps({
        "stations": len(stations),
        "duration_seconds": journey.duration_seconds,
        "parts": len(journey.parts),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
