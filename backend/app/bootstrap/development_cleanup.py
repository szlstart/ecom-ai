from __future__ import annotations

import argparse
import asyncio
import json
import re

from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.core.config import Settings, get_settings
from app.database.mysql import close_mysql, initialize_mysql, mysql_session
from app.modules.finance.account_deletion import AccountDeletionService
from app.modules.identity.models import User
from app.modules.rbac.models import Role, UserRole

_TEST_ADMIN_PATTERN = re.compile(
    r"^(?:admin|approver|initiator)_[0-9a-f]{6,32}$",
    flags=re.IGNORECASE,
)


def eligible_test_admin_username(username: str, *, keep_username: str) -> bool:
    """Return true only for the narrow test-admin names created by old acceptance runs."""
    return username != keep_username and _TEST_ADMIN_PATTERN.fullmatch(username) is not None


def assert_safe_development_database(settings: Settings) -> None:
    database_name = make_url(settings.mysql_dsn).database
    if settings.environment.lower() != "development" or database_name != "ecom_ai":
        raise RuntimeError(
            "This cleanup is restricted to the development ecom_ai database."
        )


async def run(*, keep_username: str, apply: bool) -> list[str]:
    settings = get_settings()
    assert_safe_development_database(settings)
    initialize_mysql(settings.mysql_dsn)
    requested: list[str] = []
    try:
        async for session in mysql_session():
            candidates = list(
                (
                    await session.scalars(
                        select(User)
                        .join(UserRole, UserRole.user_id == User.id)
                        .join(Role, Role.id == UserRole.role_id)
                        .where(
                            Role.role_code == "platform_super_admin",
                            UserRole.grant_status == "active",
                            User.deleted_at.is_(None),
                        )
                        .order_by(User.id)
                    )
                ).unique()
            )
            selected = [
                user
                for user in candidates
                if eligible_test_admin_username(
                    user.username,
                    keep_username=keep_username,
                )
            ]
            if apply:
                service = AccountDeletionService(session)
                for user in selected:
                    task = await service.delete_consumer(user)
                    requested.append(task.task_no)
            return [user.username for user in selected] if not apply else requested
    finally:
        await close_mysql()
    return requested


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or request deletion of legacy acceptance-test super administrators "
            "from the local development database."
        )
    )
    parser.add_argument("--keep", default="admin", help="administrator username to preserve")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="request durable account-deletion tasks; without this flag only preview",
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    result = asyncio.run(run(keep_username=arguments.keep, apply=arguments.apply))
    print(json.dumps({"mode": "apply" if arguments.apply else "preview", "items": result}))
