"""Local, resilient adapter for the two MosMetro endpoints used by planning.

The upstream endpoints are undocumented and can change without notice.  Keeping
this boundary in a separate service prevents those changes from leaking into the
main planning API.  No Telegram, Troika, MosTrans, or personal-account features
are included because this project does not use them.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query

MOSMETRO_API_URL = os.getenv("MOSMETRO_API_URL", "https://prodapp.mosmetro.ru/api")
ROUTER_API_URL = os.getenv("ROUTER_API_URL", "https://mosmetro.ru/router/api/routes")
REQUEST_TIMEOUT_SECONDS = float(os.getenv("MOSMETRO_REQUEST_TIMEOUT_SECONDS", "20"))
SCHEMA_CACHE_SECONDS = int(os.getenv("MOSMETRO_SCHEMA_CACHE_SECONDS", "3600"))
USER_AGENT = os.getenv(
    "MOSMETRO_USER_AGENT",
    "Mozilla/5.0 (compatible; SubwayRun-MosMetro-Adapter/1.0)",
)

app = FastAPI(title="SubwayRun MosMetro adapter", version="1.0")
_schema: dict[str, Any] | None = None
_schema_loaded_at = 0.0
_schema_lock = asyncio.Lock()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
    )


async def _load_schema() -> dict[str, Any]:
    """Return a cached official scheme, accepting the known envelope formats."""
    global _schema, _schema_loaded_at
    if (
        _schema is not None
        and time.monotonic() - _schema_loaded_at < SCHEMA_CACHE_SECONDS
    ):
        return _schema
    async with _schema_lock:
        if (
            _schema is not None
            and time.monotonic() - _schema_loaded_at < SCHEMA_CACHE_SECONDS
        ):
            return _schema
        try:
            async with _client() as client:
                response = await client.get(
                    f"{MOSMETRO_API_URL.rstrip('/')}/schema/v1.0"
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(
                status_code=503, detail="MosMetro schema is unavailable"
            ) from error
        candidate = (
            payload.get("data", payload) if isinstance(payload, Mapping) else None
        )
        if not isinstance(candidate, dict) or not isinstance(
            candidate.get("stations"), list
        ):
            raise HTTPException(
                status_code=503, detail="MosMetro returned an invalid schema"
            )
        _schema = candidate
        _schema_loaded_at = time.monotonic()
        return candidate


def _station_index(schema: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    return {
        int(station["id"]): station
        for station in schema.get("stations", [])
        if isinstance(station, Mapping) and station.get("id") is not None
    }


def _line_index(schema: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    return {
        int(line["id"]): line
        for line in schema.get("lines", [])
        if isinstance(line, Mapping) and line.get("id") is not None
    }


def _station_mini(
    station: Mapping[str, Any], lines: Mapping[int, Mapping[str, Any]]
) -> dict[str, Any]:
    line_id = int(station.get("lineId", 0))
    name = station.get("name") or {}
    line_name = (lines.get(line_id, {}).get("name") or {}).get("ru", "")
    return {
        "id": station["id"],
        "name": name.get("ru", str(station["id"])),
        "lineId": line_id,
        "lineName": line_name,
    }


def _connection_duration(schema: Mapping[str, Any], first: int, second: int) -> int:
    for connection in schema.get("connections", []):
        if not isinstance(connection, Mapping):
            continue
        pair = {connection.get("stationFromId"), connection.get("stationToId")}
        if pair == {first, second}:
            return max(0, int(connection.get("pathLength") or 0))
    for transition in schema.get("transitions", []):
        if not isinstance(transition, Mapping):
            continue
        pair = {transition.get("stationFromId"), transition.get("stationToId")}
        if pair == {first, second}:
            return max(0, int(transition.get("pathLength") or 0))
    return 0


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/mosmetro/schema")
async def schema() -> dict[str, Any]:
    return await _load_schema()


@app.get("/mosmetro/route")
async def route(
    from_station: int = Query(alias="from", ge=1),
    to_station: int = Query(alias="to", ge=1),
) -> dict[str, Any]:
    if from_station == to_station:
        raise HTTPException(
            status_code=404, detail="Origin and destination must differ"
        )
    schema_payload = await _load_schema()
    try:
        async with _client() as client:
            response = await client.post(
                ROUTER_API_URL,
                json={"from": from_station, "to": to_station},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(
            status_code=503, detail="MosMetro route is unavailable"
        ) from error
    variants = payload.get("data") if isinstance(payload, Mapping) else None
    if (
        not isinstance(variants, list)
        or not variants
        or not isinstance(variants[0], Mapping)
    ):
        raise HTTPException(status_code=404, detail="MosMetro route was not found")
    node_ids = [int(node) for node in variants[0].get("nodes", [])]
    stations = _station_index(schema_payload)
    lines = _line_index(schema_payload)
    nodes = [
        _station_mini(stations[node], lines) for node in node_ids if node in stations
    ]
    if len(nodes) < 2:
        raise HTTPException(
            status_code=404, detail="MosMetro route has no usable stations"
        )
    parts: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = [nodes[0]]
    for node in nodes[1:]:
        if node["lineId"] != current[-1]["lineId"]:
            parts.append({"nodes": current, "duration": 0})
            current = []
        current.append(node)
    parts.append({"nodes": current, "duration": 0})
    for part in parts:
        part["duration"] = sum(
            _connection_duration(schema_payload, int(first["id"]), int(second["id"]))
            for first, second in zip(part["nodes"], part["nodes"][1:], strict=False)
        )
    return {
        "success": True,
        "duration": max(0, int(variants[0].get("time") or 0)),
        "parts": parts,
    }
