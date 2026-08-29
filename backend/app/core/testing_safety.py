from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from app.core.config import Settings, get_settings

TEST_DATABASE_PATTERN = re.compile(r"(?:^|_)test(?:_[a-z0-9]+)?$")


def validate_integration_test_environment(
    settings: Settings,
    *,
    file_integration_enabled: bool,
) -> None:
    errors: list[str] = []
    if settings.environment.lower() != "testing":
        errors.append("ECOM_ENVIRONMENT must be 'testing'")

    mysql_database = _database_name(settings.mysql_dsn)
    if not TEST_DATABASE_PATTERN.search(mysql_database):
        errors.append("MySQL database name must end in '_test' or '_test_<run-id>'")

    postgres_database = _database_name(settings.postgres_dsn)
    if not TEST_DATABASE_PATTERN.search(postgres_database):
        errors.append("PostgreSQL database name must end in '_test' or '_test_<run-id>'")

    redis_database = _redis_database(settings.redis_url)
    if redis_database <= 0:
        errors.append("Redis integration tests must use a non-zero database number")

    if file_integration_enabled:
        if not settings.object_storage_enabled:
            errors.append("file integration tests require object storage")
        if not settings.object_storage_bucket_prefix.startswith("test-"):
            errors.append("file integration tests require a 'test-' object storage bucket prefix")

    if errors:
        detail = "\n - ".join(errors)
        raise RuntimeError(
            "Unsafe integration test environment; no test writes were allowed:\n - " + detail
        )


def validate_requested_test_environment() -> None:
    if os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1":
        return
    get_settings.cache_clear()
    validate_integration_test_environment(
        get_settings(),
        file_integration_enabled=os.getenv("ECOM_RUN_FILE_INTEGRATION_TESTS") == "1",
    )


def _database_name(dsn: str) -> str:
    return urlparse(dsn).path.lstrip("/").split("/", 1)[0]


def _redis_database(url: str) -> int:
    raw = urlparse(url).path.lstrip("/") or "0"
    try:
        return int(raw)
    except ValueError:
        return 0


if __name__ == "__main__":
    validate_requested_test_environment()
    print("Integration test environment is isolated.")
