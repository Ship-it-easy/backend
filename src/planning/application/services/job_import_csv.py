"""Strict, side effect free parsing of a job import CSV."""

import csv
import re
from collections import Counter
from datetime import datetime
from io import StringIO

HEADERS = ("Тип заявки ВК", "Начало", "Окончание", "Адрес")
FIELD_ALIASES = {
    "id": ("id", "заявка", "№ заявки"),
    "district": ("округ", "район"),
    "connection_type": ("подключение",),
    "job_status": ("статус",),
    "engineer": ("инженер", "бригада", "исполнитель"),
    "office": ("адрес офиса",),
    "work_type": ("тип заявки вк", "тип заявки"),
}


def _header_key(value: str) -> str:
    return " ".join(value.strip().lstrip("\ufeff").casefold().split()).rstrip(":")


def _detect_source_type(headers: list[str]) -> str:
    names = {_header_key(value) for value in headers}
    if any(_header_key(alias) in names for alias in FIELD_ALIASES["job_status"]):
        return "CONTROL"
    if any(_header_key(alias) in names for alias in FIELD_ALIASES["district"]):
        return "SYNTHETIC"
    if any(_header_key(alias) in names for alias in FIELD_ALIASES["office"]):
        return "SYNTHETIC"
    return "STANDARD"


def _extra_values(headers: list[str], cells: list[str]) -> dict[str, str]:
    by_key = {_header_key(header): cells[index].strip() for index, header in enumerate(headers)}
    values = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            value = by_key.get(_header_key(alias))
            if value:
                values[field] = value
                break
    return values
DATE_TIME = re.compile(
    r"^(?:\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2}) \d{1,2}:\d{2}(?::\d{2})?$"
)


def issue(code, row=None, column="Файл", value="", severity="ERROR"):
    return {
        "code": code,
        "row_number": row,
        "column": column,
        "value": str(value)[:500],
        "severity": severity,
    }


def parse_datetime(value: str):
    value = value.strip()
    if not DATE_TIME.fullmatch(value):
        return None
    for pattern in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(value, pattern).replace(second=0)
        except ValueError:
            pass
    return None


def _records(source: str, delimiter: str):
    reader = csv.reader(StringIO(source, newline=""), delimiter=delimiter, strict=True)
    previous_line = 0
    result = []
    for cells in reader:
        first_line = previous_line + 1
        previous_line = reader.line_num
        if any(cell.strip() for cell in cells):
            result.append((first_line, cells))
    return result


def _headers(cells: list[str]) -> list[str]:
    headers = [cell.strip() for cell in cells]
    if headers:
        headers[0] = headers[0].lstrip("\ufeff")
    return headers


def parse_csv(content: bytes):
    """Return encoding, delimiter, numbered rows and package/row issues."""
    decoded = []
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            decoded.append((encoding, content.decode(encoding)))
            break
        except UnicodeDecodeError:
            continue
    if not decoded:
        return None, None, [], [issue("UNSUPPORTED_ENCODING")]
    encoding, source = decoded[0]
    if encoding == "utf-8-sig" and not content.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8"
    candidates = []
    source_candidates = []
    for delimiter in (";", ","):
        try:
            records = _records(source, delimiter)
        except csv.Error:
            continue
        if not records:
            continue
        headers = _headers(records[0][1])
        normalized = {_header_key(header) for header in headers}
        aliases = {
            "Тип заявки ВК": ("тип заявки вк", "тип заявки"),
            "Начало": ("начало", "временное окно от"),
            "Окончание": ("окончание", "временное окно до"),
            "Адрес": ("адрес",),
        }
        source_headers = all(
            any(_header_key(alias) in normalized for alias in aliases[field])
            for field in HEADERS
        )
        if source_headers:
            source_candidates.append((delimiter, records, headers))
        if all(_header_key(header) in normalized for header in HEADERS):
            candidates.append((delimiter, records, headers))
    candidates = candidates or source_candidates
    if len(candidates) != 1:
        code = "UNDETERMINED_DELIMITER" if len(candidates) > 1 else "MALFORMED_CSV"
        # A recognizable header with missing columns is still a useful error.
        if not candidates:
            for delimiter in (";", ","):
                try:
                    records = _records(source, delimiter)
                except csv.Error:
                    continue
                if records and len(records[0][1]) > 1:
                    headers = _headers(records[0][1])
                    missing = [h for h in HEADERS if h not in headers]
                    if missing:
                        return (
                            encoding,
                            delimiter,
                            [],
                            [issue("MISSING_HEADER", column=h) for h in missing],
                        )
        return encoding, None, [], [issue(code)]
    delimiter, records, headers = candidates[0]
    header_aliases = {
        "тип заявки вк": {"тип заявки вк", "тип заявки"},
        "начало": {"начало", "временное окно от"},
        "окончание": {"окончание", "временное окно до"},
        "адрес": {"адрес"},
    }
    duplicate_keys = [
        key for key, count in Counter(_header_key(h) for h in headers).items()
        if count > 1 and any(key in values for values in header_aliases.values())
    ]
    duplicates = [next(h for h in headers if _header_key(h) == key) for key in duplicate_keys]
    if duplicates:
        return (
            encoding,
            delimiter,
            [],
            [issue("DUPLICATE_HEADER", column=h) for h in duplicates],
        )
    if len(records) - 1 > 1000:
        return encoding, delimiter, [], [issue("ROW_LIMIT_EXCEEDED")]
    if len(records) == 1:
        return encoding, delimiter, [], [issue("NO_DATA_ROWS")]
    rows = []
    problems = []
    canonical_header_indices = {}
    for field, aliases in {
        "Тип заявки ВК": ("тип заявки вк", "тип заявки"),
        "Начало": ("начало", "временное окно от"),
        "Окончание": ("окончание", "временное окно до"),
        "Адрес": ("адрес",),
    }.items():
        canonical_header_indices[field] = next(
            (index for index, header in enumerate(headers) if _header_key(header) in aliases),
            None,
        )
    office_row = next(
        (
            index
            for index, (_, cells) in enumerate(records[1:], start=1)
            if len(cells) == 2
            and _header_key(cells[0])
            in {_header_key(alias) for alias in FIELD_ALIASES["office"]}
        ),
        None,
    )
    source_type = _detect_source_type(headers)
    if office_row is not None:
        source_type = "SYNTHETIC"
    office_address = (
        records[office_row][1][1].strip()
        if office_row is not None and source_type == "SYNTHETIC"
        else None
    )
    if office_row is not None:
        office_line = records[office_row][0]
        records = [record for record in records if record[0] != office_line]
    for number, cells in records[1:]:
        if len(cells) != len(headers):
            rows.append(
                {
                    "row_number": number,
                    "raw_required_values_json": {h: "" for h in HEADERS},
                    "sla_date": None,
                    "time_window_start": None,
                    "time_window_end": None,
                    "issues_json": [issue("MALFORMED_CSV", number)],
                    "source_type": source_type,
                    "source_metadata_json": {},
                    "office_address": office_address,
                }
            )
            continue
        raw = {
            field: cells[index].strip() if index is not None else ""
            for field, index in canonical_header_indices.items()
        }
        extra = _extra_values(headers, cells)
        row_issues = []
        for h in HEADERS:
            if not raw[h]:
                row_issues.append(issue("REQUIRED_VALUE_MISSING", number, h))
        start = parse_datetime(raw["Начало"]) if raw["Начало"] else None
        end = parse_datetime(raw["Окончание"]) if raw["Окончание"] else None
        for h, value, parsed in (
            ("Начало", raw["Начало"], start),
            ("Окончание", raw["Окончание"], end),
        ):
            if value and parsed is None:
                row_issues.append(issue("INVALID_DATETIME", number, h, value))
        if start and end:
            if start.date() != end.date():
                row_issues.append(
                    issue("DATE_MISMATCH", number, "Окончание", raw["Окончание"])
                )
            elif end <= start:
                row_issues.append(
                    issue("INVALID_TIME_WINDOW", number, "Окончание", raw["Окончание"])
                )
        rows.append(
            {
                "row_number": number,
                "raw_required_values_json": raw,
                "sla_date": start.date() if start else None,
                "time_window_start": start.time() if start else None,
                "time_window_end": end.time() if end else None,
                "issues_json": row_issues,
                "source_type": source_type,
                "source_metadata_json": extra,
                "office_address": office_address,
            }
        )
    return encoding, delimiter, rows, problems
