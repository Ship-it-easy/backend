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
