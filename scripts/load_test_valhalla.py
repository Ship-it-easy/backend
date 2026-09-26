"""Small repeatable Valhalla route load test.

Measures the same two-point ``/route`` calls used by the public-transport
matrix provider. It intentionally reports the request count and does not
change any application data.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen
import json


POINTS = [
    (37.6176, 55.7558), (37.6350, 55.7520), (37.5700, 55.7350),
    (37.7000, 55.7800), (37.5200, 55.6900), (37.6800, 55.6500),
    (37.4500, 55.8000), (37.7600, 55.7200), (37.5900, 55.8200),
    (37.6400, 55.6800),
]


@dataclass
class Result:
    concurrency: int
    total: int
    ok: int
    failed: int
    elapsed: float
    p50: float
    p95: float
    maximum: float


async def run(base_url: str, count: int, concurrency: int, timeout: float) -> Result:
    points = POINTS[:count]
    pairs = [(a, b) for i, a in enumerate(points) for j, b in enumerate(points) if i != j]
    latencies: list[float] = []
    failures = 0
    def one(pair: tuple[tuple[float, float], tuple[float, float]]) -> float | None:
        origin, destination = pair
        payload = {"locations": [{"lon": origin[0], "lat": origin[1]},
                                  {"lon": destination[0], "lat": destination[1]}],
                   "costing": "multimodal", "units": "kilometers"}
        started = time.perf_counter()
        try:
            request = Request(base_url.rstrip("/") + "/route", data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=timeout) as response:
                response.read()
            return time.perf_counter() - started
        except Exception:
            return None

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for future in as_completed([pool.submit(one, pair) for pair in pairs]):
            value = future.result()
            (latencies if value is not None else []).append(value) if value is not None else None
    elapsed = time.perf_counter() - started
    failures = len(pairs) - len(latencies)

    ordered = sorted(latencies)
    percentile = lambda p: ordered[min(len(ordered) - 1, int(len(ordered) * p))] if ordered else 0.0
    return Result(concurrency, len(pairs), len(latencies), failures, elapsed,
                  percentile(0.50), percentile(0.95), max(ordered, default=0.0))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8002")
    parser.add_argument("--points", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 8, 16])
    args = parser.parse_args()
    if not 2 <= args.points <= len(POINTS):
        raise SystemExit(f"--points must be between 2 and {len(POINTS)}")
    print("concurrency,total,ok,failed,elapsed_s,p50_s,p95_s,max_s")
    for level in args.concurrency:
        result = await run(args.url, args.points, level, args.timeout)
        print(f"{result.concurrency},{result.total},{result.ok},{result.failed},"
              f"{result.elapsed:.3f},{result.p50:.3f},{result.p95:.3f},{result.maximum:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
