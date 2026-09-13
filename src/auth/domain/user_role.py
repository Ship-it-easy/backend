from enum import StrEnum
from logging import getLogger

logger = getLogger(__name__)


class UserRoleEnum(StrEnum):
    USER = "user"
    ADMIN = "admin"
    OWNER = "owner"
    DISPATCHER = "dispatcher"
    ENGINEER = "engineer"


def is_owner(role: UserRoleEnum) -> bool:
    return role is UserRoleEnum.OWNER


def is_dispatcher(role: UserRoleEnum) -> bool:
    return role is UserRoleEnum.DISPATCHER
