import os

import pytest
from httpx import AsyncClient

from app.bootstrap.acceptance_scenario import SCENARIO_VERSION, seed_acceptance_scenario
from app.database.mysql import mysql_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_acceptance_scenario_is_complete_and_idempotent(
    client: AsyncClient,
) -> None:
    del client
    async for session in mysql_session():
        first = await seed_acceptance_scenario(session)
        second = await seed_acceptance_scenario(session)
        assert first == second
        assert first.scenario_version == SCENARIO_VERSION
        assert first.consumer_user_id.startswith("usr_")
        assert first.merchant_user_id.startswith("usr_")
        assert first.administrator_user_id.startswith("usr_")
        assert first.store_id.startswith("sto_")
        assert first.product_id.startswith("prd_")
        assert first.sku_id.startswith("sku_")
        assert first.address_id.startswith("addr_")
