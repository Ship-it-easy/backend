from dataclasses import dataclass
from os import getenv


@dataclass
class PlanningServiceConfig:
    valhalla_url: str
    nominatim_url: str
    nominatim_viewbox: str
    geoservice_timeout_sec: float
    matrix_block_size: int

    @staticmethod
    def from_env() -> "PlanningServiceConfig":
        return PlanningServiceConfig(
            valhalla_url=getenv("VALHALLA_URL", "http://localhost:8002"),
            nominatim_url=getenv("NOMINATIM_URL", "http://localhost:8080"),
            nominatim_viewbox=getenv("NOMINATIM_VIEWBOX", "50.5,62.0,60.5,55.5"),
            geoservice_timeout_sec=float(getenv("GEOSERVICE_TIMEOUT_SEC", "15")),
            matrix_block_size=int(getenv("MATRIX_BLOCK_SIZE", "40")),
        )
