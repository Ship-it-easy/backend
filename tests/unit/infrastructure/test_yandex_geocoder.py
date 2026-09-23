import hashlib

from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.address_search_nominatim import (
    NominatimAddressSearchProvider,
    address_search_queries,
)
from planning.infrastructure.adapters.address_search_yandex import (
    YandexAddressSearchProvider,
    parse_yandex_results,
    yandex_address_search_queries,
    yandex_bbox,
)
from planning.infrastructure.adapters.geocoder_factory import (
    address_provider_version,
    create_address_search_provider,
    create_geocoder,
)
from planning.infrastructure.adapters.geocoder_nominatim import NominatimGeocoder
from planning.infrastructure.adapters.geocoder_yandex import YandexGeocoder


class _EmptyResult:
    def mappings(self):
        return self

    def one_or_none(self):
        return None


class _Session:
    def __init__(self):
        self.statements = []
        self.committed = False

    async def execute(self, statement):
        self.statements.append(statement)
        return _EmptyResult()

    async def commit(self):
        self.committed = True


def config(*, yandex: bool = False) -> PlanningServiceConfig:
    return PlanningServiceConfig(
        valhalla_url="http://valhalla",
        nominatim_url="http://nominatim",
        nominatim_viewbox="35.0,57.0,40.5,54.0",
        geoservice_timeout_sec=3,
        matrix_block_size=40,
        use_yandex_geocoder=yandex,
        yandex_geocoder_api_key="secret",
    )


def test_provider_factory_keeps_nominatim_as_default() -> None:
    settings = config()

    assert isinstance(create_geocoder(object(), settings), NominatimGeocoder)
    assert isinstance(
        create_address_search_provider(settings), NominatimAddressSearchProvider
    )
    assert address_provider_version(settings) == "nominatim-v1"


def test_provider_factory_switches_all_geocoding_to_yandex() -> None:
    settings = config(yandex=True)

    assert isinstance(create_geocoder(object(), settings), YandexGeocoder)
    assert isinstance(
        create_address_search_provider(settings), YandexAddressSearchProvider
    )
    assert address_provider_version(settings) == "yandex-geocoder-v1"


def test_yandex_bbox_falls_back_to_converted_nominatim_viewbox() -> None:
    assert yandex_bbox(config(yandex=True)) == "35.0,54.0~40.5,57.0"


def test_parse_yandex_results_maps_coordinates_and_address_components() -> None:
    payload = {
        "response": {
            "GeoObjectCollection": {
                "featureMember": [
                    {
                        "GeoObject": {
                            "uri": "ymapsbm1://geo?data=test",
                            "name": "дом 1",
                            "Point": {"pos": "37.617635 55.755814"},
                            "metaDataProperty": {
                                "GeocoderMetaData": {
                                    "Address": {
                                        "country_code": "RU",
                                        "formatted": (
                                            "Россия, Москва, улица Тестовая, 1"
                                        ),
                                        "Components": [
                                            {"kind": "country", "name": "Россия"},
                                            {"kind": "locality", "name": "Москва"},
                                            {
                                                "kind": "street",
                                                "name": "улица Тестовая",
                                            },
                                            {"kind": "house", "name": "1"},
                                        ],
                                    }
                                }
                            },
                        }
                    }
                ]
            }
        }
    }

    assert parse_yandex_results(payload) == [
        {
            "display_name": "Россия, Москва, улица Тестовая, 1",
            "latitude": 55.755814,
            "longitude": 37.617635,
            "address": {
                "country": "Россия",
                "country_code": "ru",
                "city": "Москва",
                "road": "улица Тестовая",
                "house_number": "1",
            },
            "address_key": hashlib.sha256(
                "ymapsbm1://geo?data=test".encode("utf-8")
            ).hexdigest(),
        }
    ]


def test_parse_yandex_results_ignores_non_house_results() -> None:
    payload = {
        "response": {
            "GeoObjectCollection": {
                "featureMember": [
                    {
                        "GeoObject": {
                            "name": "Москва",
                            "Point": {"pos": "37.617635 55.755814"},
                            "metaDataProperty": {
                                "GeocoderMetaData": {
                                    "Address": {
                                        "Components": [
                                            {"kind": "locality", "name": "Москва"}
                                        ]
                                    }
                                }
                            },
                        }
                    }
                ]
            }
        }
    }

    assert parse_yandex_results(payload) == []


def test_yandex_queries_compact_building_number() -> None:
    queries = yandex_address_search_queries(
        "Город Москва, ул.Талалихина, д. 2/1 к 4, кв. 387"
    )

    assert "Москва, Талалихина улица, 2/1к4" in queries


def test_yandex_results_prefer_city_over_same_street_in_another_locality() -> None:
    def item(city: str | None, longitude: str):
        components = [
            {"kind": "country", "name": "Россия"},
            {"kind": "province", "name": "Москва"},
        ]
        if city:
            components.append({"kind": "locality", "name": city})
        components.extend(
            [
                {"kind": "street", "name": "улица Талалихина"},
                {"kind": "house", "name": "16"},
            ]
        )
        return {
            "GeoObject": {
                "name": "дом 16",
                "Point": {"pos": f"{longitude} 55.7"},
                "metaDataProperty": {
                    "GeocoderMetaData": {
                        "Address": {"country_code": "RU", "Components": components}
                    }
                },
            }
        }

    payload = {
        "response": {
            "GeoObjectCollection": {
                "featureMember": [item("Москва", "37.67"), item(None, "37.58")]
            }
        }
    }

    assert [item["longitude"] for item in parse_yandex_results(payload)] == [37.67]


def test_yandex_results_prefer_city_address_over_unrequested_microdistrict() -> None:
    def item(district: str | None, longitude: str):
        components = [
            {"kind": "country", "name": "Россия"},
            {"kind": "locality", "name": "Кашира"},
        ]
        if district:
            components.append({"kind": "district", "name": district})
        components.extend(
            [
                {"kind": "street", "name": "Садовая улица"},
                {"kind": "house", "name": "18"},
            ]
        )
        return {
            "GeoObject": {
                "name": "дом 18",
                "Point": {"pos": f"{longitude} 54.8"},
                "metaDataProperty": {
                    "GeocoderMetaData": {
                        "precision": "exact",
                        "Address": {"country_code": "RU", "Components": components},
                    }
                },
            }
        }

    payload = {
        "response": {
            "GeoObjectCollection": {
                "featureMember": [
                    item(None, "38.2"),
                    item("микрорайон Ожерелье", "38.8"),
                ]
            }
        }
    }

    result = parse_yandex_results(
        payload, query="Кашира, Садовая улица, 18"
    )

    assert [item["longitude"] for item in result] == [38.2]


def test_yandex_results_accept_house_in_quarter_without_street() -> None:
    payload = {
        "response": {
            "GeoObjectCollection": {
                "featureMember": [
                    {
                        "GeoObject": {
                            "name": "к5",
                            "Point": {"pos": "37.81928 55.700045"},
                            "metaDataProperty": {
                                "GeocoderMetaData": {
                                    "Address": {
                                        "country_code": "RU",
                                        "Components": [
                                            {"kind": "country", "name": "Россия"},
                                            {"kind": "locality", "name": "Москва"},
                                            {
                                                "kind": "district",
                                                "name": (
                                                    "квартал Самаркандский Бульвар 137А"
                                                ),
                                            },
                                            {"kind": "house", "name": "к5"},
                                        ],
                                    }
                                }
                            },
                        }
                    }
                ]
            }
        }
    }

    assert parse_yandex_results(payload)[0]["address"]["locality"] == (
        "квартал Самаркандский Бульвар 137А"
    )


async def test_yandex_geocoder_calls_api_and_caches_coordinate(monkeypatch) -> None:
    payload = {
        "response": {
            "GeoObjectCollection": {
                "featureMember": [
                    {
                        "GeoObject": {
                            "name": "дом 1",
                            "Point": {"pos": "37.617635 55.755814"},
                            "metaDataProperty": {
                                "GeocoderMetaData": {
                                    "precision": "exact",
                                    "Address": {
                                        "country_code": "RU",
                                        "Components": [
                                            {"kind": "country", "name": "Россия"},
                                            {"kind": "locality", "name": "Москва"},
                                            {"kind": "street", "name": "Тестовая улица"},
                                            {"kind": "house", "name": "1"},
                                        ],
                                    },
                                }
                            },
                        }
                    }
                ]
            }
        }
    }
    request = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class Client:
        def __init__(self, **kwargs):
            request["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, path, *, params):
            request.update(path=path, params=params)
            return Response()

    monkeypatch.setattr(
        "planning.infrastructure.adapters.geocoder_yandex.httpx.AsyncClient", Client
    )
    session = _Session()

    coordinate = await YandexGeocoder(session, config(yandex=True)).geocode(
        "Москва, улица Тестовая, 1"
    )

    assert coordinate.latitude == 55.755814
    assert coordinate.longitude == 37.617635
    assert request["path"] == "/v1/"
    assert request["params"]["apikey"] == "secret"
    assert request["params"]["geocode"] in address_search_queries(
        "Москва, улица Тестовая, 1"
    )
    assert request["params"]["results"] == 10
    assert request["params"]["bbox"] == "35.0,54.0~40.5,57.0"
    assert len(session.statements) == 2
    assert session.statements[1].compile().params["provider"] == "YANDEX"
    assert session.committed is True
