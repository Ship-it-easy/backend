from planning.application.services.job_import_csv import parse_csv


def test_parses_cp1251_semicolon_and_ignores_extra_columns():
    source = (
        "Заявка;Адрес;Тип заявки ВК;Начало;Окончание\n"
        "42;Россия, Пермь, Ленина, 5;Монтаж;17.09.2026 9:05;17.09.2026 11:30\n"
    ).encode("cp1251")
    encoding, delimiter, rows, issues = parse_csv(source)
    assert (encoding, delimiter, issues) == ("cp1251", ";", [])
    assert rows[0]["row_number"] == 2
    assert rows[0]["sla_date"].isoformat() == "2026-09-17"
    assert str(rows[0]["time_window_start"]) == "09:05:00"
    assert "Заявка" not in rows[0]["raw_required_values_json"]


def test_mismatched_dates_and_partial_rows_block_import():
    source = (
        "Тип заявки ВК,Начало,Окончание,Адрес\n"
        "Монтаж,17.09.2026 9:00,18.09.2026 10:00,Пермь\n"
        "Монтаж,,17.09.2026 10:00,\n"
    ).encode()
    _, _, rows, _ = parse_csv(source)
    assert [x["code"] for x in rows[0]["issues_json"]] == ["DATE_MISMATCH"]
    assert [x["code"] for x in rows[1]["issues_json"]] == [
        "REQUIRED_VALUE_MISSING", "REQUIRED_VALUE_MISSING"
    ]


def test_quoted_newline_and_blank_record_keep_physical_line_number():
    source = (
        'Тип заявки ВК;Начало;Окончание;Адрес\n'
        '\n'
        'Монтаж;2026-09-17 9:00;2026-09-17 10:00;"Пермь,\nЛенина, 5"\n'
    ).encode()
    _, _, rows, issues = parse_csv(source)
    assert issues == []
    assert rows[0]["row_number"] == 3


def test_utf8_bom_comma_and_seconds_are_supported():
    source = (
        "Тип заявки ВК,Начало,Окончание,Адрес\r\n"
        'Монтаж,2026-09-17 9:00:59,2026-09-17 10:30:01,"Пермь, Ленина, 5"\r\n'
    ).encode("utf-8-sig")
    encoding, delimiter, rows, issues = parse_csv(source)
    assert (encoding, delimiter, issues) == ("utf-8-sig", ",", [])
    assert str(rows[0]["time_window_start"]) == "09:00:00"
    assert str(rows[0]["time_window_end"]) == "10:30:00"


def test_empty_file_duplicate_header_and_missing_header_have_specific_codes():
    header = "Тип заявки ВК;Начало;Окончание;Адрес\n".encode()
    assert [item["code"] for item in parse_csv(header)[3]] == ["NO_DATA_ROWS"]

    duplicate = "Тип заявки ВК;Начало;Окончание;Адрес;Адрес\n".encode()
    assert [item["code"] for item in parse_csv(duplicate)[3]] == [
        "DUPLICATE_HEADER"
    ]

    missing = "Тип заявки ВК;Начало;Адрес\n".encode()
    assert [item["code"] for item in parse_csv(missing)[3]] == ["MISSING_HEADER"]


def test_row_limit_counts_only_non_empty_records():
    header = "Тип заявки ВК;Начало;Окончание;Адрес\n"
    row = "Монтаж;17.09.2026 9:00;17.09.2026 10:00;Пермь, Ленина, 5\n"
    _, _, accepted, accepted_issues = parse_csv(
        (header + (row + "\n") * 1000).encode()
    )
    assert len(accepted) == 1000
    assert accepted_issues == []

    _, _, rejected, rejected_issues = parse_csv((header + row * 1001).encode())
    assert rejected == []
    assert [item["code"] for item in rejected_issues] == ["ROW_LIMIT_EXCEEDED"]


def test_timezone_offset_and_equal_window_are_rejected():
    source = (
        "Тип заявки ВК;Начало;Окончание;Адрес\n"
        "Монтаж;2026-09-17 09:00+05:00;2026-09-17 10:00;Пермь, Ленина, 5\n"
        "Монтаж;17.09.2026 9:00;17.09.2026 9:00;Пермь, Ленина, 6\n"
    ).encode()
    _, _, rows, issues = parse_csv(source)
    assert issues == []
    assert [item["code"] for item in rows[0]["issues_json"]] == [
        "INVALID_DATETIME"
    ]
    assert [item["code"] for item in rows[1]["issues_json"]] == [
        "INVALID_TIME_WINDOW"
    ]


def test_malformed_quotes_and_extra_cells_are_rejected():
    malformed = (
        "Тип заявки ВК;Начало;Окончание;Адрес\n"
        'Монтаж;17.09.2026 9:00;17.09.2026 10:00;"Пермь\n'
    ).encode()
    assert [item["code"] for item in parse_csv(malformed)[3]] == ["MALFORMED_CSV"]

    extra = (
        "Тип заявки ВК;Начало;Окончание;Адрес\n"
        "Монтаж;17.09.2026 9:00;17.09.2026 10:00;Пермь;лишнее\n"
    ).encode()
    _, _, rows, issues = parse_csv(extra)
    assert issues == []
    assert [item["code"] for item in rows[0]["issues_json"]] == ["MALFORMED_CSV"]
