import hashlib
import io
import os
import secrets
from decimal import Decimal
from urllib.parse import unquote, urlparse

import httpx
import pyotp
import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select

from app.bootstrap.admin import provision_platform_super_admin
from app.core.config import get_settings
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import mysql_session
from app.integrations.object_storage import get_object_storage
from app.modules.files.models import FileObject
from app.modules.files.processor import FileProcessor
from app.modules.files.scanner import ClamAvScanner
from app.modules.identity.models import User
from app.modules.stores.models import Store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_FILE_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_FILE_INTEGRATION_TESTS=1 with isolated MySQL and MinIO",
    ),
]


class AcceptAllScanner:
    async def scan(self, payload: bytes) -> None:
        assert payload


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (1200, 800), (14, 116, 144)).save(output, format="PNG")
    return output.getvalue()


async def test_clamav_stream_protocol_accepts_a_clean_payload() -> None:
    await ClamAvScanner(get_settings()).scan(b"ecom-ai file scanner health payload")


async def test_presigned_upload_scan_derivation_and_store_logo_binding(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    settings = get_settings()
    security = SecurityService(settings)
    password = f"Admin-File-{suffix}-Correct-Horse!"

    async for session in mysql_session():
        provisioning = await provision_platform_super_admin(
            session,
            security,
            username=f"file_admin_{suffix}",
            password=password,
        )
        owner = await session.scalar(select(User).where(User.user_no == provisioning.user_no))
        assert owner is not None
        store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=owner.id,
            store_name=f"文件测试店铺 {suffix}",
            store_name_normalized=f"file-store-{suffix}",
            store_status="active",
            rating_score=Decimal("0.00"),
            rating_count=0,
            follower_count=0,
            sales_count=0,
            opened_at=utc_now(),
        )
        session.add(store)
        await session.commit()
        store_no = store.store_no

    login = await client.post(
        "/api/v1/admin/auth/login",
        json={
            "identifier": f"file_admin_{suffix}",
            "password": password,
            "client": {"client_type": "web", "device_name": "File Admin Test"},
        },
    )
    assert login.status_code == 200, login.text
    mfa = await client.post(
        "/api/v1/admin/auth/mfa-verifications",
        headers={"Idempotency-Key": f"file-mfa-{suffix}-0001"},
        json={
            "challenge_id": login.json()["data"]["challenge_id"],
            "method": "totp",
            "code": pyotp.TOTP(provisioning.totp_secret).now(),
        },
    )
    assert mfa.status_code == 200, mfa.text
    auth = {"Authorization": f"Bearer {mfa.json()['data']['session']['access_token']}"}

    policy = await client.get("/api/v1/file-upload-policies/store_logo")
    assert policy.status_code == 200
    assert policy.json()["data"]["max_count"] == 1

    payload = _png()
    digest = hashlib.sha256(payload).hexdigest()
    created = await client.post(
        "/api/v1/file-upload-sessions",
        headers={**auth, "Idempotency-Key": f"file-create-{suffix}-0001"},
        json={
            "purpose": "store_logo",
            "filename": "logo.png",
            "size_bytes": len(payload),
            "content_type": "image/png",
            "sha256": digest,
            "business_context_id": store_no,
        },
    )
    assert created.status_code == 201, created.text
    upload = created.json()["data"]
    assert upload["upload_status"] == "created"
    async with httpx.AsyncClient(trust_env=False) as network:
        put = await network.put(
            upload["upload"]["url"],
            content=payload,
            headers=upload["upload"]["headers"],
        )
    assert put.status_code == 200, put.text

    completed = await client.post(
        f"/api/v1/file-upload-sessions/{upload['upload_id']}/complete",
        headers={**auth, "Idempotency-Key": f"file-complete-{suffix}-01"},
        json={"sha256": digest, "provider_checksum": None},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["data"]["source_file"]["scan_status"] == "pending"
    assert completed.json()["data"]["bindable_file"] is None

    storage = get_object_storage()
    temporary_path = unquote(urlparse(upload["upload"]["url"]).path).lstrip("/")
    temporary_bucket, temporary_key = temporary_path.split("/", 1)
    with pytest.raises(ApplicationError) as removed:
        await storage.stat(temporary_bucket, temporary_key)
    assert removed.value.status == 404
    source_file_id = completed.json()["data"]["source_file"]["file_id"]
    async for session in mysql_session():
        source = await session.scalar(
            select(FileObject).where(FileObject.file_no == source_file_id)
        )
        assert source is not None
        assert source.bucket == "private-image-sources"

    async for session in mysql_session():
        processed = await FileProcessor(session, storage, AcceptAllScanner()).process_batch()
    assert processed == 1

    ready = await client.get(f"/api/v1/file-upload-sessions/{upload['upload_id']}", headers=auth)
    assert ready.status_code == 200, ready.text
    bindable = ready.json()["data"]["bindable_file"]
    assert bindable["status"] == "active"
    assert bindable["scan_status"] == "safe"
    assert bindable["content_type"] == "image/webp"

    store_before = await client.get(f"/api/v1/admin/stores/{store_no}", headers=auth)
    updated = await client.patch(
        f"/api/v1/admin/stores/{store_no}",
        headers={**auth, "If-Match": store_before.headers["etag"]},
        json={"logo_file_id": bindable["file_id"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["logo_file_id"] == bindable["file_id"]
    assert updated.json()["data"]["logo_url"] == bindable["url"]

    redirect = await client.get(bindable["url"], follow_redirects=False)
    assert redirect.status_code == 307
    async with httpx.AsyncClient(trust_env=False) as network:
        downloaded = await network.get(redirect.headers["location"])
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("image/webp")

    wrong_purpose = await client.post(
        "/api/v1/admin/brands",
        headers={**auth, "Idempotency-Key": f"brand-wrong-file-{suffix}"},
        json={
            "brand_name": f"错误用途品牌 {suffix}",
            "logo_file_id": bindable["file_id"],
            "description": None,
        },
    )
    assert wrong_purpose.status_code == 422
    assert wrong_purpose.json()["code"] == "FILE_NOT_BINDABLE"

    import_payload = b"product_key,product_name\nP001,Import test\n"
    import_digest = hashlib.sha256(import_payload).hexdigest()
    import_created = await client.post(
        "/api/v1/file-upload-sessions",
        headers={**auth, "Idempotency-Key": f"import-file-create-{suffix}-01"},
        json={
            "purpose": "admin_import",
            "filename": "products.csv",
            "size_bytes": len(import_payload),
            "content_type": "text/csv",
            "sha256": import_digest,
            "business_context_id": store_no,
        },
    )
    assert import_created.status_code == 201, import_created.text
    import_upload = import_created.json()["data"]
    async with httpx.AsyncClient(trust_env=False) as network:
        put_import = await network.put(
            import_upload["upload"]["url"],
            content=import_payload,
            headers=import_upload["upload"]["headers"],
        )
    assert put_import.status_code == 200, put_import.text
    import_completed = await client.post(
        f"/api/v1/file-upload-sessions/{import_upload['upload_id']}/complete",
        headers={**auth, "Idempotency-Key": f"import-file-complete-{suffix}-01"},
        json={"sha256": import_digest, "provider_checksum": None},
    )
    assert import_completed.status_code == 200, import_completed.text
    assert import_completed.json()["data"]["bindable_file"] is None

    async for session in mysql_session():
        processed_import = await FileProcessor(session, storage, AcceptAllScanner()).process_batch()
    assert processed_import == 1
    import_ready = await client.get(
        f"/api/v1/file-upload-sessions/{import_upload['upload_id']}", headers=auth
    )
    assert import_ready.status_code == 200, import_ready.text
    import_bindable = import_ready.json()["data"]["bindable_file"]
    assert import_bindable["file_id"] == import_ready.json()["data"]["source_file"]["file_id"]
    assert import_bindable["variant"] == "original"
    assert import_bindable["status"] == "active"
    assert import_bindable["scan_status"] == "safe"
    assert import_bindable["url"] is None
