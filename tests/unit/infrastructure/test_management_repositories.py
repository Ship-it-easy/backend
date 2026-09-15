from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from auth.domain.user_role import UserRoleEnum
from planning.application.errors import ConflictError
from planning.infrastructure.adapters.admin_management_repositories_sqla import (
    SqlaAdminUserRepository,
)
from planning.infrastructure.adapters.project_management_repositories_sqla import (
    SqlaEngineerAccountRepository,
)


class DatabaseError(Exception):
    def __init__(self, constraint_name: str):
        self.diag = SimpleNamespace(constraint_name=constraint_name)


def integrity_error(constraint_name: str) -> IntegrityError:
    return IntegrityError("INSERT", {}, DatabaseError(constraint_name))


def user_row(**changes):
    values = {
        "id": "00000000-0000-0000-0000-000000000001",
        "username": "same-login",
        "password_hash": "hash",
        "is_active": True,
        "role": UserRoleEnum.OWNER,
        "is_verified": True,
        "project_id": None,
        "engineer_id": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


async def test_admin_create_user_returns_normal_result_without_committing() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    result = MagicMock()
    result.mappings.return_value.one.return_value = user_row()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()

    created = await SqlaAdminUserRepository(session).create_user(
        "same-login", "hash", UserRoleEnum.OWNER
    )

    assert created["login"] == "same-login"
    session.commit.assert_not_awaited()


async def test_admin_create_user_maps_username_constraint_and_rolls_back() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=integrity_error("uq_users_username_ci"))
    session.rollback = AsyncMock()

    with pytest.raises(ConflictError) as raised:
        await SqlaAdminUserRepository(session).create_user(
            "same-login", "hash", UserRoleEnum.OWNER
        )

    assert raised.value.code == "LOGIN_EXISTS"
    session.rollback.assert_awaited_once()


async def test_admin_create_user_does_not_mask_other_constraint() -> None:
    error = integrity_error("users_engineer_id_key")
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=error)
    session.rollback = AsyncMock()

    with pytest.raises(IntegrityError) as raised:
        await SqlaAdminUserRepository(session).create_user(
            "same-login", "hash", UserRoleEnum.ENGINEER, 1, 2
        )

    assert raised.value is error
    session.rollback.assert_awaited_once()


async def test_engineer_create_account_maps_username_constraint() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=integrity_error("uq_users_username_ci"))
    session.rollback = AsyncMock()
    repository = SqlaEngineerAccountRepository(session)
    repository._account = AsyncMock(return_value=None)

    with pytest.raises(ConflictError) as raised:
        await repository.create_account(1, 2, "same-login", "hash")

    assert raised.value.code == "LOGIN_EXISTS"
    session.rollback.assert_awaited_once()


async def test_engineer_create_account_preserves_engineer_conflict() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    session.execute = AsyncMock(side_effect=integrity_error("users_engineer_id_key"))
    session.rollback = AsyncMock()
    repository = SqlaEngineerAccountRepository(session)
    repository._account = AsyncMock(return_value=None)

    with pytest.raises(ConflictError) as raised:
        await repository.create_account(1, 2, "login", "hash")

    assert raised.value.code == "ACCESS_EXISTS"
