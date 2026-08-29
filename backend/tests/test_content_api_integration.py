from __future__ import annotations

import os
import secrets

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.bootstrap.admin import provision_platform_super_admin
from app.core.config import get_settings
from app.core.security import SecurityService
from app.database.mysql import mysql_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


async def test_admin_content_version_publish_public_projection_and_withdrawal(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    username = f"content_admin_{suffix}"
    password = f"Content-Admin-{suffix}-Correct-Horse!"
    async for session in mysql_session():
        isolation = await session.scalar(text("SELECT @@transaction_isolation"))
        assert str(isolation).upper() == "READ-COMMITTED"
        provisioning = await provision_platform_super_admin(
            session,
            SecurityService(get_settings()),
            username=username,
            password=password,
        )

    login = await client.post(
        "/api/v1/admin/auth/login",
        json={
            "identifier": username,
            "password": password,
            "client": {"client_type": "web", "device_name": "Content Acceptance"},
        },
    )
    assert login.status_code == 200, login.text
    mfa = await client.post(
        "/api/v1/admin/auth/mfa-verifications",
        headers={"Idempotency-Key": f"content-mfa-{suffix}-001"},
        json={
            "challenge_id": login.json()["data"]["challenge_id"],
            "method": "totp",
            "code": pyotp.TOTP(provisioning.totp_secret).now(),
        },
    )
    assert mfa.status_code == 200, mfa.text
    auth = {"Authorization": f"Bearer {mfa.json()['data']['session']['access_token']}"}

    content_key = f"help.acceptance-{suffix}"
    malicious = await client.post(
        "/api/v1/admin/content",
        headers=auth,
        json={
            "content_key": f"help.malicious-{suffix}",
            "content_type": "help_article",
            "title": "不安全内容",
            "locale": "zh-CN",
            "region_code": "CN",
            "source_format": "html",
            "source_content": "<p>正文</p><script>alert(1)</script>",
        },
    )
    assert malicious.status_code == 422

    created = await client.post(
        "/api/v1/admin/content",
        headers=auth,
        json={
            "content_key": content_key,
            "content_type": "help_article",
            "title": "验收帮助",
            "locale": "zh-CN",
            "region_code": "CN",
            "source_format": "html",
            "source_content": "<h2>帮助</h2><p>第一版内容</p>",
        },
    )
    assert created.status_code == 201, created.text
    content_id = created.json()["data"]["content_id"]
    detail = await client.get(f"/api/v1/admin/content/{content_id}", headers=auth)
    assert detail.status_code == 200, detail.text
    assert detail.headers["etag"] == '"0"'

    missing_precondition = await client.put(
        f"/api/v1/admin/content/{content_id}",
        headers=auth,
        json={
            "title": "验收帮助第二版",
            "locale": "zh-CN",
            "region_code": "CN",
            "source_format": "html",
            "source_content": "<h2>帮助</h2><p>第二版内容</p>",
        },
    )
    assert missing_precondition.status_code == 428
    updated = await client.put(
        f"/api/v1/admin/content/{content_id}",
        headers={**auth, "If-Match": detail.headers["etag"]},
        json={
            "title": "验收帮助第二版",
            "locale": "zh-CN",
            "region_code": "CN",
            "source_format": "html",
            "source_content": "<h2>帮助</h2><p>第二版内容</p>",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["versions"][0]["version"] == "v2"
    published = await client.post(
        f"/api/v1/admin/content/{content_id}/versions/v2/publish", headers=auth
    )
    assert published.status_code == 200, published.text

    public_list = await client.get("/api/v1/content/help-articles")
    assert public_list.status_code == 200
    assert content_key in [item["content_key"] for item in public_list.json()["data"]["items"]]
    public_detail = await client.get(f"/api/v1/content/help-articles/{content_key}")
    assert public_detail.status_code == 200, public_detail.text
    public_version = public_detail.json()["data"]["version"]
    assert public_version["version"] == "v2"
    assert public_version["text"] == "帮助 第二版内容"
    assert "script" not in public_detail.text.casefold()

    listing = await client.get("/api/v1/admin/content", headers=auth)
    assert listing.status_code == 200
    assert content_id in [item["content_id"] for item in listing.json()["data"]["items"]]
    withdrawn = await client.post(f"/api/v1/admin/content/{content_id}/withdraw", headers=auth)
    assert withdrawn.status_code == 200
    gone = await client.get(f"/api/v1/content/help-articles/{content_key}")
    assert gone.status_code == 410
    assert gone.json()["code"] == "CONTENT_GONE"
