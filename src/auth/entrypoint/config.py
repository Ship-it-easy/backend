from dataclasses import dataclass
from os import getenv

from dotenv import load_dotenv

load_dotenv()


@dataclass
class SessionConfig:
    expiration_minutes: int

    @staticmethod
    def from_env() -> "SessionConfig":
        expiration_minutes = getenv("SESSION_EXPIRATION_MINUTES")
        return SessionConfig(expiration_minutes=int(expiration_minutes))


@dataclass
class PostgresConfig:
    host: str
    port: int
    db: str
    user: str
    password: str
    uri: str

    @staticmethod
    def from_env() -> "PostgresConfig":
        host = getenv("POSTGRES_HOST")
        port = getenv("POSTGRES_PORT")
        db = getenv("POSTGRES_DB")
        user = getenv("POSTGRES_USER")
        password = getenv("POSTGRES_PASS")

        uri = f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"

        return PostgresConfig(
            uri=uri, host=host, port=port, db=db, user=user, password=password
        )


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


@dataclass
class Config:
    postgres_config: PostgresConfig
    session_config: SessionConfig
    planning_service_config: PlanningServiceConfig
    # RabbitMQConfig полностью удален отсюда


def create_config() -> Config:
    return Config(
        postgres_config=PostgresConfig.from_env(),
        session_config=SessionConfig.from_env(),
        planning_service_config=PlanningServiceConfig.from_env(),
    )
