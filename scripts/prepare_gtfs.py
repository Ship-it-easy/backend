"""Prepare a downloaded GTFS ZIP for Valhalla's /gtfs_feeds/moscow mount.

Keep only standard columns. The Mobility Database export also contains portal
metadata and an unnamed trailing column, which are not part of GTFS Schedule.
The source archive is read-only; output is local and excluded from Git.
"""

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
from datetime import date, timedelta
from pathlib import Path
from zipfile import ZipFile

FIELDS = {
    "agency.txt": ("agency_id", "agency_name", "agency_url", "agency_timezone"),
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
    "routes.txt": (
        "route_id",
        "agency_id",
        "route_short_name",
        "route_long_name",
        "route_type",
        "route_desc",
    ),
    "stops.txt": ("stop_id", "stop_name", "stop_lat", "stop_lon"),
    "stop_times.txt": (
        "trip_id",
        "arrival_time",
        "departure_time",
        "stop_id",
        "stop_sequence",
        "pickup_type",
        "drop_off_type",
    ),
    "trips.txt": (
        "route_id",
        "service_id",
        "trip_id",
        "trip_headsign",
        "direction_id",
        "block_id",
    ),
    "calendar_dates.txt": ("service_id", "date", "exception_type"),
    "shapes.txt": (
        "shape_id",
        "shape_pt_lat",
        "shape_pt_lon",
        "shape_pt_sequence",
        "shape_dist_traveled",
    ),
    "transfers.txt": (
        "from_stop_id",
        "to_stop_id",
        "transfer_type",
        "min_transfer_time",
    ),
    "frequencies.txt": (
        "trip_id",
        "start_time",
        "end_time",
        "headway_secs",
        "exact_times",
    ),
}
REQUIRED = set(FIELDS) - {
    "calendar_dates.txt", "shapes.txt", "transfers.txt", "frequencies.txt"
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare(source: Path, destination: Path, horizon_days: int = 30) -> dict:
    if horizon_days < 1:
        raise ValueError("horizon_days must be positive")
    archive_hash = _sha256(source)
    marker = destination / "source.json"
    if destination.exists():
        if marker.exists():
            old = json.loads(marker.read_text(encoding="utf-8"))
            if (
                old.get("sha256") == archive_hash
                and old.get("horizon_days") == horizon_days
                and all((destination / name).is_file() for name in REQUIRED)
            ):
                return old
        raise FileExistsError(
            f"{destination} already holds another or incomplete feed; "
            "stop Valhalla and move it aside before importing a replacement"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(source) as archive:
        missing = REQUIRED - set(archive.namelist())
        if missing:
            raise ValueError(f"Missing GTFS files: {', '.join(sorted(missing))}")
        today = date.today()
        first_day = today.strftime("%Y%m%d")
        last_day = (today + timedelta(days=horizon_days - 1)).strftime("%Y%m%d")
        with archive.open("calendar.txt") as binary:
            calendars_reader = csv.DictReader(
                io.TextIOWrapper(binary, encoding="utf-8-sig")
            )
            valid_services = {
                row["service_id"]
                for row in calendars_reader
                if row["start_date"] <= last_day
                and row["end_date"] >= first_day
                and any(
                    row[day] == "1"
                    for day in (
                        "monday",
                        "tuesday",
                        "wednesday",
                        "thursday",
                        "friday",
                        "saturday",
                        "sunday",
                    )
                )
            }
        # Some upstream stop_times rows refer to deleted trips or stops.
        # Limit to the application's planning horizon and connected references.
        with archive.open("trips.txt") as binary:
            trips_reader = csv.DictReader(
                io.TextIOWrapper(binary, encoding="utf-8-sig")
            )
            trip_ids = {
                row["trip_id"]
                for row in trips_reader
                if row["service_id"] in valid_services
            }
        with archive.open("stops.txt") as binary:
            stops_reader = csv.DictReader(
                io.TextIOWrapper(binary, encoding="utf-8-sig")
            )
            stop_ids = {row["stop_id"] for row in stops_reader}
        with tempfile.TemporaryDirectory(
            prefix=".gtfs-import-", dir=destination.parent
        ) as temporary:
            output = Path(temporary)
            counts = {}
            skipped = {"missing_trip": 0, "missing_stop": 0}
            active_services = set()
            last_service_date = first_day
            current_day = today.strftime("%Y%m%d")
            weekday = today.strftime("%A").lower()
            for name, fields in FIELDS.items():
                if name not in archive.namelist():
                    continue
                with (
                    archive.open(name) as binary,
                    (output / name).open("w", encoding="utf-8", newline="") as target,
                ):
                    reader = csv.DictReader(
                        io.TextIOWrapper(binary, encoding="utf-8-sig")
                    )
                    missing_columns = set(fields[:2]) - set(reader.fieldnames or ())
                    if missing_columns:
                        raise ValueError(f"{name}: missing columns {missing_columns}")
                    selected = tuple(
                        field for field in fields if field in reader.fieldnames
                    )
                    writer = csv.DictWriter(target, fieldnames=selected)
                    writer.writeheader()
                    count = 0
                    for row in reader:
                        if (
                            name == "calendar.txt"
                            and row["service_id"] not in valid_services
                        ):
                            continue
                        if name == "trips.txt" and row["trip_id"] not in trip_ids:
                            continue
                        if name == "frequencies.txt" and row["trip_id"] not in trip_ids:
                            continue
                        if name == "stop_times.txt":
                            if row["trip_id"] not in trip_ids:
                                skipped["missing_trip"] += 1
                                continue
                            if row["stop_id"] not in stop_ids:
                                skipped["missing_stop"] += 1
                                continue
                        writer.writerow({field: row[field] or "" for field in selected})
                        count += 1
                        if name == "calendar.txt" and (
                            row["start_date"] <= current_day <= row["end_date"]
                            and row[weekday] == "1"
                        ):
                            active_services.add(row["service_id"])
                        if name == "calendar.txt":
                            last_service_date = max(last_service_date, row["end_date"])
                counts[name] = count
            if not active_services:
                raise ValueError(f"No active service on {today}; feed may be outdated")
            if any(counts[name] == 0 for name in REQUIRED):
                raise ValueError("One or more required GTFS files are empty")
            report = {
                "source": source.name,
                "sha256": archive_hash,
                "prepared_on": today.isoformat(),
                "horizon_days": horizon_days,
                "selection_window_end": last_day,
                "last_service_date": last_service_date,
                "active_services_today": len(active_services),
                "rows": counts,
                "skipped_stop_times": skipped,
            }
            (output / "source.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if os.name == "nt":
                # Docker Desktop's bind mount needs read access to files created
                # under TemporaryDirectory's otherwise private Windows ACL.
                subprocess.run(
                    ["icacls", str(output), "/grant", "*S-1-1-0:(OI)(CI)RX", "/T"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
            else:
                output.chmod(0o755)
            shutil.move(str(output), str(destination))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Downloaded GTFS ZIP")
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "gtfs" / "moscow",
    )
    parser.add_argument("--horizon-days", type=int, default=30)
    arguments = parser.parse_args()
    print(
        json.dumps(
            prepare(arguments.source, arguments.destination, arguments.horizon_days),
            indent=2,
        )
    )
