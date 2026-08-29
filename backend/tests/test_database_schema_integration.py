from __future__ import annotations

import os

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.database.mysql import mysql_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_mysql_uses_read_committed_and_microsecond_datetime_columns(
    client: AsyncClient,
) -> None:
    _ = client
    async for session in mysql_session():
        isolation = await session.scalar(text("SELECT @@transaction_isolation"))
        precision_drift = await session.scalar(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND DATA_TYPE = 'datetime'
                  AND DATETIME_PRECISION <> 6
                """
            )
        )
        assert str(isolation).upper() == "READ-COMMITTED"
        assert precision_drift == 0
