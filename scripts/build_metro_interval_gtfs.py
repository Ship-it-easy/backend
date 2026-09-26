"""Build a clearly labelled interval-based GTFS model from the MosMetro scheme.

Station order, coordinates, links and first/last departures come from the local
MosMetro wrapper. Connection pathLength is treated as estimated seconds, as the
wrapper does; headways are editable assumptions in config/metro_headways.json.
No individual train departure time is claimed to be an official timetable.
"""

import argparse
import csv
import hashlib
import json
import shutil
import tempfile
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {
    "agency.txt": ("agency_id", "agency_name", "agency_url", "agency_timezone"),
    "stops.txt": ("stop_id", "stop_name", "stop_lat", "stop_lon"),
    "routes.txt": (
        "route_id",
        "agency_id",
        "route_short_name",
        "route_long_name",
        "route_type",
    ),
    "calendar.txt": (
        "service_id",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "start_date",
        "end_date",
    ),
    "trips.txt": (
        "route_id",
        "service_id",
        "trip_id",
        "trip_headsign",
        "direction_id",
        "shape_id",
    ),
    "stop_times.txt": (
        "trip_id",
        "arrival_time",
        "departure_time",
        "stop_id",
        "stop_sequence",
        "shape_dist_traveled",
    ),
    "frequencies.txt": (
        "trip_id",
        "start_time",
        "end_time",
        "headway_secs",
        "exact_times",
    ),
    "transfers.txt": (
        "from_stop_id",
        "to_stop_id",
        "transfer_type",
        "min_transfer_time",
    ),
    "shapes.txt": (
        "shape_id",
        "shape_pt_lat",
        "shape_pt_lon",
        "shape_pt_sequence",
        "shape_dist_traveled",
    ),
}


def seconds(clock: str) -> int:
    hour, minute = (int(value) for value in clock.split(":"))
    if not 0 <= hour <= 47 or not 0 <= minute < 60:
        raise ValueError(f"Invalid GTFS time: {clock}")
    return hour * 3600 + minute * 60


def gtfs_time(value: int) -> str:
    hour, remainder = divmod(value, 3600)
    minute, second = divmod(remainder, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def station_stop(station_id: int) -> str:
    return f"metro_{station_id}"


def validate_profile(profile: dict) -> None:
    def validate_windows(day: str, windows: list[dict]) -> None:
        previous_end = None
        for window in windows:
            start, end = seconds(window["start"]), seconds(window["end"])
            if start >= end or int(window["headway_seconds"]) <= 0:
                raise ValueError(f"Invalid {day} headway window: {window}")
            if previous_end is not None and start != previous_end:
                raise ValueError(f"{day} headway windows must be contiguous")
            previous_end = end

    for day in ("weekday", "weekend"):
        validate_windows(day, profile[day])
    for line_id, overrides in profile.get("line_overrides", {}).items():
        for day, windows in overrides.items():
            if day not in ("weekday", "weekend"):
                raise ValueError(f"Unknown service type for line {line_id}: {day}")
            validate_windows(f"line {line_id} {day}", windows)


def path_between(start: int, end: int, graph: dict[int, dict[int, int]]) -> list[int]:
    previous = {start: None}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        if current == end:
            break
        for neighbor in sorted(graph[current]):
            if neighbor not in previous:
                previous[neighbor] = current
                queue.append(neighbor)
    if end not in previous:
        raise ValueError(f"Disconnected metro line: {start} -> {end}")
    path = []
    current = end
    while current is not None:
        path.append(current)
        current = previous[current]
    return path[::-1]


def line_paths(graph: dict[int, dict[int, int]], start_hint: int) -> list[list[int]]:
    terminals = sorted(node for node, neighbors in graph.items() if len(neighbors) == 1)
    if terminals:
        if len(terminals) == 2:
            return [path_between(*terminals, graph)]
        # The scheme does not publish train service patterns on branches. Use
        # the declared line start as the common trunk and disclose it.
        root = start_hint if start_hint in terminals else terminals[0]
        return [path_between(root, end, graph) for end in terminals if end != root]
    if any(len(neighbors) != 2 for neighbors in graph.values()):
        raise ValueError("Unsupported metro line topology")
    start = start_hint if start_hint in graph else min(graph)
    path = [start]
    previous = None
    current = start
    while True:
        neighbors = sorted(graph[current])
        following = neighbors[0] if neighbors[0] != previous else neighbors[1]
        path.append(following)
        if following == start:
            break
        if len(path) > len(graph):
            raise ValueError("Metro loop did not close")
        previous, current = current, following
    if len(path) != len(graph) + 1:
        raise ValueError("Metro loop omitted stations")
    return [path]


def service_limits(station: dict, next_id: int, weekend: bool) -> tuple[int, int]:
    timetable = station.get("scheduleTrains") or {}
    rows = timetable.get(str(next_id)) or []
    rows = [row for row in rows if row.get("weekend") is weekend]
    if not rows:
        return seconds("05:30"), seconds("25:00")
    parsed = []
    for row in rows:
        try:
            # Some terminal records append a destination after the clock.
            parsed.append(
                (seconds(row["first"].split()[0]), seconds(row["last"].split()[0]))
            )
        except (ValueError, AttributeError, IndexError):
            continue
    if not parsed:
        return seconds("05:30"), seconds("25:00")
    first = min(item[0] for item in parsed)
    last = max(item[1] for item in parsed)
    if last < first:
        last += 24 * 3600
    return first, last + 60  # GTFS end_time is exclusive.


def generate(schema: dict, profile: dict, output: Path, horizon_days: int) -> dict:
    validate_profile(profile)
    if horizon_days < 1:
        raise ValueError("horizon_days must be positive")
    stations = {
        int(row["id"]): row
        for row in schema["stations"]
        if not row.get("perspective") and row.get("location")
    }
    graphs = defaultdict(lambda: defaultdict(dict))
    for connection in schema["connections"]:
        a, b = int(connection["stationFromId"]), int(connection["stationToId"])
        if connection.get("perspective") or a not in stations or b not in stations:
            continue
        line = int(stations[a]["lineId"])
        if line != int(stations[b]["lineId"]):
            continue
        duration = int(connection["pathLength"])
        if duration <= 0:
            raise ValueError(f"Missing travel duration for {a} -> {b}")
        graphs[line][a][b] = duration
        if connection.get("bi", True):
            graphs[line][b][a] = duration
    rows = {name: [] for name in FIELDS}
    rows["agency.txt"].append(
        {
            "agency_id": "metro_model",
            "agency_name": "Модель метро Москвы (оценка)",
            "agency_url": "https://mosmetro.ru/",
            "agency_timezone": "Europe/Moscow",
        }
    )
    for station in stations.values():
        rows["stops.txt"].append(
            {
                "stop_id": station_stop(int(station["id"])),
                "stop_name": station["name"]["ru"],
                "stop_lat": station["location"]["lat"],
                "stop_lon": station["location"]["lon"],
            }
        )
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    through = today + timedelta(days=horizon_days - 1)
    for service, weekdays in (
        ("weekday", (1, 1, 1, 1, 1, 0, 0)),
        ("weekend", (0, 0, 0, 0, 0, 1, 1)),
    ):
        rows["calendar.txt"].append(
            dict(
                zip(
                    FIELDS["calendar.txt"],
                    (
                        service,
                        *weekdays,
                        today.strftime("%Y%m%d"),
                        through.strftime("%Y%m%d"),
                    ),
                    strict=True,
                )
            )
        )
    branched_lines = []
    excluded_rail_lines = []
    for line in schema["lines"]:
        line_id = int(line["id"])
        graph = graphs.get(line_id)
        if line.get("perspective") or not graph:
            continue
        line_name = line["name"]["ru"]
        # MCC and MCD are rail services with different operating patterns.
        # Do not present them as subway with the subway headway assumptions.
        if line_name == "МЦК" or line_name.startswith("МЦД"):
            excluded_rail_lines.append({"id": line_id, "name": line_name})
            continue
        route_id = f"metro_line_{line_id}"
        rows["routes.txt"].append(
            {
                "route_id": route_id,
                "agency_id": "metro_model",
                "route_short_name": str(line_id),
                "route_long_name": line_name,
                "route_type": 1,
            }
        )
        paths = line_paths(graph, int(line.get("stationStartId") or 0))
        if len(paths) > 1:
            branched_lines.append(line_id)
        for path_index, path in enumerate(paths):
            for direction, stations_path in enumerate((path, path[::-1])):
                trip_prefix = f"metro_{line_id}_{path_index}_{direction}"
                shape_id = f"shape_{trip_prefix}"
                elapsed = 0
                stop_offsets = []
                for sequence, station_id in enumerate(stations_path, 1):
                    station = stations[station_id]
                    if sequence > 1:
                        previous = stations_path[sequence - 2]
                        elapsed += graph[previous][station_id]
                    rows["shapes.txt"].append(
                        {
                            "shape_id": shape_id,
                            "shape_pt_lat": station["location"]["lat"],
                            "shape_pt_lon": station["location"]["lon"],
                            "shape_pt_sequence": sequence,
                            "shape_dist_traveled": "",
                        }
                    )
                    stop_offsets.append((station_id, elapsed))
                for service in ("weekday", "weekend"):
                    earliest, latest = service_limits(
                        stations[stations_path[0]],
                        stations_path[1],
                        service == "weekend",
                    )
                    windows = (
                        profile.get("line_overrides", {})
                        .get(str(line_id), {})
                        .get(service, profile[service])
                    )
                    for window_index, window in enumerate(windows):
                        begin = max(earliest, seconds(window["start"]))
                        end = min(latest, seconds(window["end"]))
                        if begin >= end:
                            continue
                        trip_id = f"{trip_prefix}_{service}_{window_index}"
                        rows["trips.txt"].append(
                            {
                                "route_id": route_id,
                                "service_id": service,
                                "trip_id": trip_id,
                                "trip_headsign": stations[stations_path[-1]]["name"][
                                    "ru"
                                ],
                                "direction_id": direction,
                                "shape_id": shape_id,
                            }
                        )
                        for sequence, (station_id, offset) in enumerate(
                            stop_offsets, 1
                        ):
                            rows["stop_times.txt"].append(
                                {
                                    "trip_id": trip_id,
                                    "arrival_time": gtfs_time(
                                        seconds("05:30") + offset
                                    ),
                                    "departure_time": gtfs_time(
                                        seconds("05:30") + offset
                                    ),
                                    "stop_id": station_stop(station_id),
                                    "stop_sequence": sequence,
                                    "shape_dist_traveled": "",
                                }
                            )
                        rows["frequencies.txt"].append(
                            {
                                "trip_id": trip_id,
                                "start_time": gtfs_time(begin),
                                "end_time": gtfs_time(end),
                                "headway_secs": int(window["headway_seconds"]),
                                "exact_times": 0,
                            }
                        )
    transfer_pairs = set()
    for transfer in schema["transitions"]:
        a, b = int(transfer["stationFromId"]), int(transfer["stationToId"])
        if transfer.get("perspective") or a not in stations or b not in stations:
            continue
        duration = int(transfer["pathLength"])
        if duration <= 0:
            continue
        pairs = [(a, b)] + ([(b, a)] if transfer.get("bi", True) else [])
        for first, second in pairs:
            if (first, second) in transfer_pairs:
                continue
            transfer_pairs.add((first, second))
            rows["transfers.txt"].append(
                {
                    "from_stop_id": station_stop(first),
                    "to_stop_id": station_stop(second),
                    "transfer_type": 2,
                    "min_transfer_time": duration,
                }
            )
    if not rows["frequencies.txt"] or not rows["stop_times.txt"]:
        raise ValueError("The metro scheme produced no service")
    output.mkdir()
    for name, fields in FIELDS.items():
        with (output / name).open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows[name])
    report = {
        "quality": "estimated_interval_model_not_official_timetable",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "valid_from": today.isoformat(),
        "valid_through": through.isoformat(),
        "headway_profile": profile,
        "travel_time_source": (
            "MosMetro scheme connections.pathLength, interpreted as seconds "
            "by the wrapper; unvalidated"
        ),
        "first_last_source": "MosMetro scheme station.scheduleTrains, when available",
        "branch_service_assumption": (
            "declared line start to each other terminal; "
            "no direct branch-to-branch train"
        ),
        "branched_lines": branched_lines,
        "excluded_rail_lines": excluded_rail_lines,
        "rows": {name: len(items) for name, items in rows.items()},
    }
    (output / "model_info.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema-url", default="http://127.0.0.1:8082/mosmetro/schema")
    parser.add_argument(
        "--headways", type=Path, default=ROOT / "config" / "metro_headways.json"
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=ROOT / "data" / "gtfs" / "moscow_metro_model",
    )
    # The interval model is a recurring weekly pattern, not a finite timetable.
    # Keep the calendar usable for long-range planning; refresh station topology
    # independently when the source scheme changes.
    parser.add_argument("--horizon-days", type=int, default=1826)
    parser.add_argument(
        "--replace", action="store_true", help="Keep previous feed in a dated backup"
    )
    args = parser.parse_args()
    profile = json.loads(args.headways.read_text(encoding="utf-8"))
    with urlopen(args.schema_url, timeout=30) as response:
        schema_bytes = response.read()
    schema = json.loads(schema_bytes)
    destination = args.destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not args.replace:
        raise FileExistsError(f"{destination} exists; use --replace to keep a backup")
    with tempfile.TemporaryDirectory(
        prefix="metro-gtfs-", dir=destination.parent
    ) as temporary:
        output = Path(temporary) / "feed"
        report = generate(schema, profile, output, args.horizon_days)
        report["schema_sha256"] = hashlib.sha256(schema_bytes).hexdigest()
        (output / "model_info.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if destination.exists():
            marker = destination / "model_info.json"
            if not marker.is_file() or json.loads(
                marker.read_text(encoding="utf-8")
            ).get("quality") != "estimated_interval_model_not_official_timetable":
                raise FileExistsError(
                    f"Refusing to replace unrelated GTFS at {destination}"
                )
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = destination.with_name(f"{destination.name}-backup-{stamp}.zip")
            if backup.exists():
                raise FileExistsError(f"Backup already exists: {backup}")
            shutil.make_archive(
                str(backup.with_suffix("")), "zip", root_dir=destination
            )
            shutil.rmtree(destination)
            print(f"Previous feed preserved at {backup}")
        shutil.move(str(output), str(destination))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
