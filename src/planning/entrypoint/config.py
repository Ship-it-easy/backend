from dataclasses import dataclass
from os import getenv


def _env_flag(name: str, default: bool = False) -> bool:
    value = getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


@dataclass
class PlanningServiceConfig:
    valhalla_url: str
    nominatim_url: str
    nominatim_viewbox: str
    geoservice_timeout_sec: float
    matrix_block_size: int
    use_yandex_geocoder: bool = False
    yandex_geocoder_url: str = "https://geocode-maps.yandex.ru"
    yandex_geocoder_api_key: str = ""
    yandex_geocoder_bbox: str = ""
    traffic_model_enabled: bool = True
    mosmetro_url: str = ""
    mosmetro_max_access_meters: int = 2500
    mosmetro_waiting_seconds: int = 180

    @staticmethod
    def from_env() -> "PlanningServiceConfig":
        return PlanningServiceConfig(
            valhalla_url=getenv("VALHALLA_URL", "http://localhost:8002"),
            nominatim_url=getenv("NOMINATIM_URL", "http://localhost:8080"),
            nominatim_viewbox=getenv("NOMINATIM_VIEWBOX", "35.0,57.0,40.5,54.0"),
            geoservice_timeout_sec=float(getenv("GEOSERVICE_TIMEOUT_SEC", "15")),
            matrix_block_size=int(getenv("MATRIX_BLOCK_SIZE", "40")),
            use_yandex_geocoder=_env_flag("USE_YANDEX_GEOCODER"),
            yandex_geocoder_url=getenv(
                "YANDEX_GEOCODER_URL", "https://geocode-maps.yandex.ru"
            ),
            yandex_geocoder_api_key=getenv("YANDEX_GEOCODER_API_KEY", ""),
            yandex_geocoder_bbox=getenv("YANDEX_GEOCODER_BBOX", ""),
            traffic_model_enabled=_env_flag("TRAFFIC_MODEL_ENABLED", True),
            mosmetro_url=getenv("MOSMETRO_URL", ""),
            mosmetro_max_access_meters=int(
                getenv("MOSMETRO_MAX_ACCESS_METERS", "2500")
            ),
            mosmetro_waiting_seconds=int(getenv("MOSMETRO_WAITING_SECONDS", "180")),
        )
