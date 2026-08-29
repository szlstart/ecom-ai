from __future__ import annotations

import argparse
import asyncio
import getpass

from app.bootstrap.merchant import provision_store_operator, provisioning_result_json
from app.core.config import get_settings
from app.core.security import SecurityService
from app.database.mysql import close_mysql, initialize_mysql, mysql_session


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision a store-scoped merchant operator")
    parser.add_argument("username", help="unique merchant username")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--store-id", help="public ID of an existing store")
    target.add_argument("--store-name", help="create a local active store with this name")
    return parser.parse_args()


async def run(
    username: str,
    password: str,
    *,
    store_id: str | None,
    store_name: str | None,
) -> None:
    settings = get_settings()
    initialize_mysql(settings.mysql_dsn)
    try:
        async for session in mysql_session():
            result = await provision_store_operator(
                session,
                SecurityService(settings),
                username=username,
                password=password,
                store_no=store_id,
                store_name=store_name,
            )
            print(provisioning_result_json(result))
    finally:
        await close_mysql()


if __name__ == "__main__":
    arguments = parse_args()
    secret = getpass.getpass("Merchant password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if secret != confirmation:
        raise SystemExit("password confirmation does not match")
    asyncio.run(
        run(
            arguments.username,
            secret,
            store_id=arguments.store_id,
            store_name=arguments.store_name,
        )
    )
