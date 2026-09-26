"""Check Yandex Geocoder access without printing the API key."""

import asyncio

import httpx

from planning.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.adapters.address_search_yandex import (
    parse_yandex_results,
    yandex_request_params,
)

_ADDRESS = "Москва, Тверская улица, 13"


async def main() -> None:
    config = PlanningServiceConfig.from_env()
    if not config.use_yandex_geocoder:
        raise SystemExit("Установите USE_YANDEX_GEOCODER=true")
    if not config.yandex_geocoder_api_key or config.yandex_geocoder_api_key.startswith(
        "REPLACE_WITH"
    ):
        raise SystemExit("Укажите рабочий YANDEX_GEOCODER_API_KEY")

    try:
        async with httpx.AsyncClient(
            base_url=config.yandex_geocoder_url,
            timeout=config.geoservice_timeout_sec,
        ) as client:
            response = await client.get(
                "/v1/",
                params=yandex_request_params(config, _ADDRESS, results=10),
            )
    except httpx.HTTPError as error:
        raise SystemExit(
            f"Не удалось связаться с API Геокодера: {type(error).__name__}"
        ) from None

    if response.status_code != 200:
        raise SystemExit(f"API Геокодера вернул HTTP {response.status_code}")
    try:
        candidates = parse_yandex_results(
            response.json(),
            query=_ADDRESS,
            require_house=True,
            accepted_precisions={"exact"},
        )
    except ValueError:
        raise SystemExit("API Геокодера вернул неожиданный ответ") from None
    if not candidates:
        raise SystemExit("API доступен, но контрольный адрес не найден точно")
    print(f"Яндекс-геокодер работает: {candidates[0]['display_name']}")


if __name__ == "__main__":
    asyncio.run(main())
