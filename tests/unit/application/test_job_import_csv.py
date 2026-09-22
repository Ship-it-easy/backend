from planning.application.services.job_import_csv import parse_csv
from planning.infrastructure.adapters.address_search_nominatim import address_search_queries


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


def test_accepts_reference_export_latin_bk_header():
    source = (
        "Тип заявки BK;Начало;Окончание;Адрес;Район\n"
        "Монтаж;17.09.2026 9:05;17.09.2026 11:30;Москва, Ленина, 5;Центр\n"
    ).encode("cp1251")
    encoding, delimiter, rows, issues = parse_csv(source)
    assert (encoding, delimiter, issues) == ("cp1251", ";", [])
    assert rows[0]["raw_required_values_json"]["Тип заявки ВК"] == "Монтаж"


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


def test_normalizes_reference_address_for_nominatim_building_search():
    queries = address_search_queries(
        "Город Москва, пр-кт.Волгоградский, д. 128 к 5, кв. 1"
    )
    assert queries == (
        "Москва, Волгоградский проспект, 128 к 5",
        "Город Москва, пр-кт.Волгоградский, д. 128 к 5, кв. 1",
    )


def test_normalizes_osm_building_structure_notation():
    queries = address_search_queries(
        "Город Москва, ул.Международная, д. 28 стр. 1, кв. 186"
    )
    assert queries[0] == "Москва, Международная улица, 28 с1"
