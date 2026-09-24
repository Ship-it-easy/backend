"""Read source exports, geocode building addresses locally, audit model coverage.

Never treats appointment windows or the control assignment as observed travel.
Only source filenames/hashes, building addresses and anonymous counts are saved.
"""

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from planning.domain.traffic import covered  # noqa: E402


def building_address(value):
    return re.sub(r",?\s*кв\.?\s*[\w/-]+\s*$", "", value.strip(), flags=re.I)


def queries(value):
    value = building_address(value)
    value = re.sub(r"^(?:обл\.Московская область,\s*|МО,\s*)", "", value, flags=re.I)
    value = re.sub(r"\bг\.\s*", "", value, flags=re.I)
    value = re.sub(r"\bгород\s+", "", value, flags=re.I)
    value = re.sub(r"^(Москва|Кашира|Ступино|Домодедово)\s+(?!,)", r"\1, ", value)
    value = re.sub(r"\bпгт\.", "", value, flags=re.I)
    for short, long in (
        (r"пр-кт", "проспект"),
        (r"пр-зд|пр-д", "проезд"),
        (r"ул", "улица"),
        (r"пер", "переулок"),
        (r"наб", "набережная"),
        (r"б-р", "бульвар"),
    ):
        value = re.sub(rf"\b(?:{short})(?:\.\s*|\s+)", long + " ", value, flags=re.I)
    value = re.sub(r"\b(проезд|шоссе)\.\s*", r"\1 ", value, flags=re.I)
    value = re.sub(r",?\s*\bд(?:\.\s*|\s+)(?=\d)", ", ", value, flags=re.I)
    value = re.sub(r"\s+к\s*(\d+)", r"к\1", value, flags=re.I)
    value = re.sub(
        r"\s*стр\.?\s*(\d+)|(?<=\d)с\s*(\d+)",
        lambda m: " с" + (m[1] or m[2]),
        value,
        flags=re.I,
    )
    value = re.sub(
        r"(улица|проспект|проезд|набережная|переулок|бульвар)\s+([^,]+)",
        r"\2 \1",
        value,
        flags=re.I,
    )
    value = re.sub(r"\s*,\s*", ", ", value).strip(" ,")
    ordinal = re.sub(r"([^,]+) (1-й|2-й|3-й) (проезд|улица)", r"\2 \1 \3", value)
    return list(dict.fromkeys([value, ordinal]))


def get_json(url, params):
    with urllib.request.urlopen(
        url + "?" + urllib.parse.urlencode(params), timeout=15
    ) as response:
        return json.load(response)


def geocode(address, districts=()):
    candidates = []
    for query in queries(address):
        candidates = get_json(
            "http://localhost:8080/search",
            {
                "q": query,
                "format": "jsonv2",
                "limit": 10,
                "addressdetails": 1,
                "countrycodes": "ru",
                "viewbox": "36.85,56.15,38.65,54.65",
                "bounded": 1,
            },
        )
        buildings = [c for c in candidates if c.get("address", {}).get("house_number")]
        expected = query.rsplit(",", 1)[-1].strip().casefold()

        def house_key(value):
            return re.sub(r"[\s,.]", "", value.casefold())

        exact = [
            c
            for c in buildings
            if house_key(c["address"]["house_number"]) == house_key(expected)
        ]
        if exact:
            buildings = exact
        district_candidates = [
            c
            for c in buildings
            if any(
                d.casefold().replace("ё", "е")
                in c["display_name"].casefold().replace("ё", "е")
                for d in districts
                if d
            )
        ]
        if district_candidates:
            buildings = district_candidates
        unique = {}
        for c in buildings:
            a = c["address"]
            key = (
                a.get("road", a.get("pedestrian")),
                a["house_number"].casefold(),
                a.get("city", a.get("town", a.get("village"))),
            )
            unique.setdefault(key, c)
        # POIs and building entrances can have different road aliases. They must
        # refer to the same building number and lie within ~20 m to be collapsed.
        if len(unique) > 1:
            values = list(unique.values())
            first = values[0]
            if all(
                c["address"]["house_number"].casefold()
                == first["address"]["house_number"].casefold()
                and abs(float(c["lat"]) - float(first["lat"])) < 0.00018
                and abs(float(c["lon"]) - float(first["lon"])) < 0.00030
                for c in values
            ):
                unique = {"same_building": first}
        if len(unique) == 1:
            found = next(iter(unique.values()))
            lat, lon = float(found["lat"]), float(found["lon"])
            return {
                "address": address,
                "status": "building_match",
                "query": query,
                "latitude": lat,
                "longitude": lon,
                "covered": covered(lat, lon),
                "matched_address": found["display_name"],
            }
    return {
        "address": address,
        "status": "ambiguous" if candidates else "not_found",
        "candidates": len(candidates),
        "queries": queries(address),
        "candidate_addresses": [c.get("display_name") for c in candidates],
        "area_covered": all(
            covered(float(c["lat"]), float(c["lon"])) for c in candidates
        )
        if candidates
        else None,
        "requires_coordinate_review": True,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--geocode", action="store_true")
    args = parser.parse_args()
    files, addresses, offices, records = [], set(), [], []
    districts_by_address = {}
    for path in sorted(args.directory.glob("*.csv")):
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
            encoding = "utf-8-sig"
        except UnicodeDecodeError:
            text, encoding = raw.decode("cp1251"), "cp1251"
        rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
        jobs = [r for r in rows if str(r.get("Заявка", "")).strip().isdigit()]
        for row in rows:
            if "офис" in str(row.get("Заявка", "")).casefold():
                office = next(
                    (v for k, v in row.items() if k != "Заявка" and v.strip()), None
                )
                if office:
                    offices.append(
                        {"file": path.name, "address": building_address(office)}
                    )
                    addresses.add(building_address(office))
        for row in jobs:
            address = building_address(row["Адрес"])
            addresses.add(address)
            districts_by_address.setdefault(address, set()).add(row["Район"])
            records.append(
                {
                    "file": path.name,
                    "address": address,
                    "district": row["Район"],
                    "window_start": row["Начало"],
                    "window_end": row["Окончание"],
                }
            )
        files.append(
            {
                "name": path.name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "encoding": encoding,
                "jobs": len(jobs),
                "other_rows": len(rows) - len(jobs),
                "districts": dict(sorted(Counter(r["Район"] for r in jobs).items())),
                "columns": list(rows[0]),
                "has_observed_travel_times": False,
            }
        )
    output = ROOT / "data" / "traffic" / "coverage_audit.json"
    cached = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    matches = {
        r["address"]: r
        for r in cached.get("addresses", [])
    }
    if args.geocode:
        missing = sorted(
            a for a in addresses
            if matches.get(a, {}).get('status') != 'building_match'
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            for index, result in enumerate(
                pool.map(
                    lambda a: geocode(a, districts_by_address.get(a, ())), missing
                ),
                1,
            ):
                matches[result["address"]] = result
                if index % 20 == 0:
                    print(f"Geocoded {index}/{len(missing)}", flush=True)
    results = [
        matches.get(address, {"address": address, "status": "not_geocoded"})
        for address in sorted(addresses)
    ]
    for r in results:
        if "latitude" in r:
            r["covered"] = covered(r["latitude"], r["longitude"])
    report = {
        "sources": files,
        "total_job_rows": len(records),
        "unique_building_address_strings": len(addresses),
        "offices": offices,
        "districts": sorted({r["district"] for r in records}),
        "observed_trip_count": 0,
        "accuracy_percent": None,
        "addresses": results,
        "records": records,
        "geocoding_counts": dict(Counter(r["status"] for r in results)),
        "covered_building_matches": sum(r.get("covered", False) for r in results),
    }
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# Проверка покрытия шести CSV",
        "",
        "Шаг модели — 5 минут. Территория: Москва, Домодедово, Ступино, Кашира "
        "и связующие дороги. Географическое покрытие "
        "не означает измеренную точность ETA.",
        "",
        "| Файл | Строк заявок | Прочих строк |",
        "| --- | ---: | ---: |",
    ]
    lines.extend(f"| {f['name']} | {f['jobs']} | {f['other_rows']} |" for f in files)
    lines += [
        "",
        f"Всего: {len(records)} строк заявок и {len(offices)} офиса. "
        f"Различных адресных строк без квартир: {len(addresses)}.",
        "",
        "Однозначных сопоставлений зданий: "
        f"{report['geocoding_counts'].get('building_match', 0)}; "
        f"из них в зоне модели: {report['covered_building_matches']}.",
        "",
        "Точность времени не измерена: фактических поездок в источниках — 0. "
        "Поля Начало/Окончание не использовались как длительности дороги.",
        "",
        "## Адреса, которым нужны подтверждённые координаты",
        "",
        "Для этих адресов нельзя гарантировать точное место прибытия. "
        "Подстановка центра улицы или соседнего корпуса не выполнялась.",
        "",
        "| Адрес | Результат локального геокодера |",
        "| --- | --- |",
    ]
    for entry in results:
        if entry["status"] != "building_match":
            status = (
                "Дом не найден"
                if entry["status"] == "not_found"
                else "Нет однозначного здания"
            )
            lines.append(f"| {entry['address']} | {status} |")
    lines += [
        "",
        "Полные результаты и хеши источников: "
        "`data/traffic/coverage_audit.json`. Проверка дорожных путей: "
        "`data/traffic/route_coverage_check.json`. "
        "Контрольные и синтетические файлы не суммируются как независимые поездки.",
        "",
    ]
    (ROOT / "docs/TRAFFIC_COVERAGE_2026.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in {"addresses", "records", "sources"}
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
