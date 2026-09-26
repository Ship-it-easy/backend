from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from planning.domain.traffic import MOSCOW
from planning.infrastructure.adapters.mosmetro import (
    MetroJourney,
    MetroStation,
    build_metro_map_candidate,
    metro_matrix_candidate,
    metro_operating_window,
)
from planning.infrastructure.adapters.traffic_route import evaluate_trip
from tests.unit.infrastructure.test_traffic import trip


class FakeMetro:
    def __init__(self):
        self.first = MetroStation(1, "Первая", 1, 55.8, 37.4)
        self.second = MetroStation(2, "Вторая", 1, 55.9, 37.5)
        self.by_id = {1: self.first, 2: self.second}

    async def nearest(self, latitude, longitude, max_distance_meters):
        return self.first if latitude < 55.85 else self.second

    async def route(self, origin_id, destination_id):
        return MetroJourney(
            600,
            [
                {
                    "duration": 600,
                    "nodes": [
                        {"id": 1, "name": "Первая", "lineName": "Линия 1"},
                        {"id": 2, "name": "Вторая", "lineName": "Линия 1"},
                    ],
                }
            ],
        )

    async def station(self, station_id):
        return self.by_id.get(station_id)


class TransferMetro(FakeMetro):
    async def route(self, origin_id, destination_id):
        return MetroJourney(
            800,
            [
                {
                    "duration": 300,
                    "nodes": [
                        {"id": 1, "name": "Первая", "lineName": "Линия 1"},
                        {"id": 2, "name": "Вторая", "lineName": "Линия 1"},
                    ],
                },
                {
                    "duration": 400,
                    "nodes": [
                        {"id": 2, "name": "Вторая", "lineName": "Линия 2"},
                        {"id": 1, "name": "Первая", "lineName": "Линия 2"},
                    ],
                },
            ],
        )


def test_api_fallback_rejects_night_and_trip_past_closing():
    assert not metro_operating_window(datetime(2026, 9, 25, 2, tzinfo=MOSCOW))
    assert not metro_operating_window(
        datetime(2026, 9, 25, 0, 50, tzinfo=MOSCOW), 20 * 60
    )
    assert metro_operating_window(datetime(2026, 9, 25, 8, tzinfo=MOSCOW))


@pytest.mark.asyncio
async def test_metro_matrix_includes_walk_wait_and_rail_distance():
    result = await metro_matrix_candidate(
        FakeMetro(),
        (55.799, 37.399),
        (55.901, 37.501),
        2500,
        180,
        datetime(2026, 9, 25, 8, tzinfo=MOSCOW),
    )
    assert result is not None
    seconds, meters = result
    assert seconds > 780
    assert meters > 10_000


@pytest.mark.asyncio
async def test_map_candidate_contains_walk_and_metro_segments():
    valhalla = AsyncMock()
    valhalla.post.return_value = httpx.Response(
        200,
        request=httpx.Request("POST", "http://valhalla/route"),
        json={"trip": trip(120, road="Пешком")},
    )
    candidate = await build_metro_map_candidate(
        FakeMetro(),
        valhalla,
        (55.799, 37.399),
        (55.901, 37.501),
        datetime(2026, 9, 25, 8, tzinfo=MOSCOW),
        evaluate_trip,
        2500,
        180,
    )
    assert candidate is not None
    assert candidate["provider"] == "MOSMETRO_HYBRID"
    assert candidate["duration_seconds"] == 1020
    metro = [s for s in candidate["segments"] if s["travel_type"] == "metro"]
    assert metro[0]["transit"] == {
        "route": "Линия 1",
        "from_stop": "Первая",
        "to_stop": "Вторая",
    }
    assert valhalla.post.call_count == 2


@pytest.mark.asyncio
async def test_map_candidate_places_transfer_before_next_line():
    valhalla = AsyncMock()
    valhalla.post.return_value = httpx.Response(
        200,
        request=httpx.Request("POST", "http://valhalla/route"),
        json={"trip": trip(120, road="Пешком")},
    )
    departure = datetime(2026, 9, 25, 8, tzinfo=MOSCOW)
    candidate = await build_metro_map_candidate(
        TransferMetro(),
        valhalla,
        (55.799, 37.399),
        (55.901, 37.501),
        departure,
        evaluate_trip,
        2500,
        180,
    )
    assert candidate is not None
    transfer = next(
        segment
        for segment in candidate["segments"]
        if segment["travel_type"] == "transfer"
    )
    second_line = [
        segment
        for segment in candidate["segments"]
        if (segment.get("transit") or {}).get("route") == "Линия 2"
    ][0]
    assert transfer["duration_seconds"] == 100
    second_departure = datetime.fromisoformat(second_line["departure_at"])
    transfer_departure = datetime.fromisoformat(transfer["departure_at"])
    assert second_departure == transfer_departure + timedelta(seconds=100)
