"""Strict, side effect free parsing of a job import CSV."""

import csv
import re
from collections import Counter
from datetime import datetime
from io import StringIO

HEADERS = ("Тип заявки ВК", "Начало", "Окончание", "Адрес")
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
    for delimiter in (";", ","):
        try:
            records = _records(source, delimiter)
        except csv.Error:
            continue
        if not records:
            continue
        headers = [cell.strip().lstrip("\ufeff") for cell in records[0][1]]
        if all(header in headers for header in HEADERS):
            candidates.append((delimiter, records, headers))
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
                    headers = [cell.strip().lstrip("\ufeff") for cell in records[0][1]]
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
    duplicates = [h for h, n in Counter(headers).items() if h in HEADERS and n > 1]
    if duplicates:
        return (
            encoding,
            delimiter,
            [],
            [issue("DUPLICATE_HEADER", column=h) for h in duplicates],
        )
    if len(records) - 1 > 1000:
        return encoding, delimiter, [], [issue("ROW_LIMIT_EXCEEDED")]
    rows = []
    problems = []
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
                }
            )
            continue
        raw = {h: cells[headers.index(h)].strip() for h in HEADERS}
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
            }
        )
    return encoding, delimiter, rows, problems
