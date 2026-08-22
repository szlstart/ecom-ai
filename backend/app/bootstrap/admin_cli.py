from __future__ import annotations

import argparse
import asyncio
import getpass

from app.bootstrap.admin import provision_platform_super_admin, provisioning_result_json
from app.core.config import get_settings
from app.core.security import SecurityService
from app.database.mysql import close_mysql, initialize_mysql, mysql_session


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provision an initial platform super administrator"
    )
    parser.add_argument("username", help="unique administrator username")
    return parser.parse_args()


async def run(username: str, password: str) -> None:
    settings = get_settings()
    initialize_mysql(settings.mysql_dsn)
    try:
        async for session in mysql_session():
            result = await provision_platform_super_admin(
                session,
                SecurityService(settings),
                username=username,
                password=password,
            )
            print(provisioning_result_json(result))
    finally:
        await close_mysql()


if __name__ == "__main__":
    arguments = parse_args()
    secret = getpass.getpass("Administrator password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if secret != confirmation:
        raise SystemExit("password confirmation does not match")
    asyncio.run(run(arguments.username, secret))
