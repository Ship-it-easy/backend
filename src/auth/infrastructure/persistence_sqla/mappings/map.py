from auth.infrastructure.persistence_sqla.mappings.session import map_sessions_table
from auth.infrastructure.persistence_sqla.mappings.user import map_users_table
from planning.infrastructure.persistence_sqla.mappings import (  # noqa: F401
    tables as planning_tables,
)


def map_tables() -> None:
    map_users_table()
    map_sessions_table()
