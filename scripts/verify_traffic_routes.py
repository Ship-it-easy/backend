"""Check real local road paths to every unambiguous source address, both ways."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx

from planning.domain.traffic import MOSCOW, VERSION, covered
from planning.infrastructure.adapters.traffic_route import evaluate_trip

ROOT = Path(__file__).resolve().parents[1]


def main():
    audit = json.loads(
        (ROOT / "data/traffic/coverage_audit.json").read_text(encoding="utf-8")
    )
    matches = {
        r["address"]: r for r in audit["addresses"] if r["status"] == "building_match"
    }
    office = next(
        matches[o["address"]] for o in audit["offices"] if o["address"] in matches
    )
    # A connectivity/coverage probe, not a reconstructed engineer assignment.
    pairs = [(office, row) for row in matches.values() if row != office]
    pairs += [(b, a) for a, b in pairs]
    departure = datetime(2026, 8, 17, 8, tzinfo=MOSCOW)
    with httpx.Client(base_url="http://localhost:8002", timeout=30) as client:

        def verify(pair):
            origin, destination = pair
            response = client.post(
                "/route",
                json={
                    "locations": [
                        {"lat": p["latitude"], "lon": p["longitude"]} for p in pair
                    ],
                    "costing": "auto",
                    "date_time": {"type": 3, "value": "2026-08-17T08:00"},
                    "costing_options": {"auto": {"speed_types": ["freeflow"]}},
                    "language": "ru-RU",
                },
            )
            response.raise_for_status()
            estimate = evaluate_trip(response.json()["trip"], departure, "auto")
            return {
                "origin": origin["address"],
                "destination": destination["address"],
                "baseline_seconds": estimate["baseline_seconds"],
                "estimated_seconds": estimate["duration_seconds"],
                "uncovered_seconds": estimate["uncovered_baseline_seconds"],
                "all_shape_points_covered": all(
                    covered(*p) for p in estimate["points"]
                ),
                "profiles": sorted(
                    {s["corridor_id"] for s in estimate["segments"] if s["corridor_id"]}
                ),
            }

        results = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            for index, result in enumerate(pool.map(verify, pairs), 1):
                results.append(result)
                if index % 40 == 0:
                    print(f"Checked {index}/{len(pairs)} road routes", flush=True)
    report = {
        "model_version": VERSION,
        "probe": "one_verified_office_to_each_building_and_back",
        "tested_routes": len(results),
        "fully_covered_routes": sum(r["all_shape_points_covered"] for r in results),
        "addresses_requiring_review": len(audit["addresses"]) - len(matches),
        "accuracy_percent": None,
        "note": "Geographic coverage is not ETA accuracy.",
        "routes": results,
    }
    (ROOT / "data/traffic/route_coverage_check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in report.items() if k != "routes"}))


if __name__ == "__main__":
    main()
