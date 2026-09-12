from enum import StrEnum
from logging import getLogger

logger = getLogger(__name__)


class UserRoleEnum(StrEnum):
    ADMIN = "admin"
    USER = "user"
    OWNER = "owner"
    DISPATCHER = "dispatcher"
    ENGINEER = "engineer"


def has_required_role(user_role: UserRoleEnum, required_role: UserRoleEnum) -> bool:
    if required_role is UserRoleEnum.USER:
        return user_role in {
            UserRoleEnum.USER,
            UserRoleEnum.ADMIN,
            UserRoleEnum.OWNER,
            UserRoleEnum.DISPATCHER,
            UserRoleEnum.ENGINEER,
        }

    if required_role is UserRoleEnum.ADMIN:
        return user_role in {UserRoleEnum.ADMIN, UserRoleEnum.OWNER}

    return user_role is required_role


def is_owner(role: UserRoleEnum) -> bool:
    return role in {UserRoleEnum.OWNER, UserRoleEnum.ADMIN}


def is_dispatcher(role: UserRoleEnum) -> bool:
    return role in {UserRoleEnum.DISPATCHER, UserRoleEnum.USER, UserRoleEnum.ADMIN}
