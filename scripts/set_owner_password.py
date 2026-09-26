"""Set a new password for the seeded owner account on a fresh installation."""

import asyncio
from getpass import getpass

import bcrypt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from auth.entrypoint.config import PostgresConfig


async def set_password(password_hash: str) -> None:
    engine = create_async_engine(PostgresConfig.from_env().uri)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE users
                    SET password_hash = :password_hash
                    WHERE lower(username) = 'owner' AND role::text = 'OWNER'
                    RETURNING id
                    """
                ),
                {"password_hash": password_hash},
            )
            if result.scalar_one_or_none() is None:
                raise RuntimeError("Owner account was not found; check the migrations")
    finally:
        await engine.dispose()


def main() -> None:
    password = getpass("Новый пароль owner (не менее 12 символов): ")
    password_bytes = password.encode()
    if len(password) < 12 or len(password_bytes) > 72:
        raise SystemExit("Пароль: не менее 12 символов и не более 72 байт")
    if password != getpass("Повторите пароль: "):
        raise SystemExit("Пароли не совпадают")
    password_hash = bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode()
    asyncio.run(set_password(password_hash))
    print("Пароль owner обновлён")


if __name__ == "__main__":
    main()
