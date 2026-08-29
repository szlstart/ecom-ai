import csv
import hashlib
import io
import os
import secrets
from datetime import timedelta
from decimal import Decimal

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.bootstrap.admin import provision_platform_super_admin
from app.core.config import get_settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import SecurityService, utc_now
from app.database.mysql import mysql_session
from app.integrations.object_storage import ObjectMetadata
from app.modules.batch_jobs.parser import HEADERS
from app.modules.batch_jobs.processor import BatchJobProcessor
from app.modules.catalog.models import Category, Product, ProductSku
from app.modules.files.models import FileObject
from app.modules.identity.models import User
from app.modules.inventory.models import Inventory
from app.modules.stores.models import Store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with an isolated database",
    ),
]


class MemoryObjectStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}

    async def probe(self) -> None:
        return None

    async def ensure_bucket(self, _bucket: str) -> None:
        return None

    async def presign_put(self, bucket: str, object_key: str, _expires: timedelta) -> str:
        return f"memory://{bucket}/{object_key}"

    async def presign_get(self, bucket: str, object_key: str, _expires: timedelta) -> str:
        return f"memory://{bucket}/{object_key}"

    async def stat(self, bucket: str, object_key: str) -> ObjectMetadata:
        payload, content_type = self.objects[(bucket, object_key)]
        return ObjectMetadata(
            size=len(payload),
            content_type=content_type,
            etag=hashlib.md5(payload, usedforsecurity=False).hexdigest(),
            version_id="memory-v1",
            metadata={},
        )

    async def read(self, bucket: str, object_key: str, max_bytes: int) -> bytes:
        payload = self.objects[(bucket, object_key)][0]
        assert len(payload) <= max_bytes
        return payload

    async def put(
        self, bucket: str, object_key: str, data: bytes, content_type: str
    ) -> ObjectMetadata:
        self.objects[(bucket, object_key)] = (data, content_type)
        return await self.stat(bucket, object_key)

    async def remove(self, bucket: str, object_key: str) -> None:
        self.objects.pop((bucket, object_key), None)


def _csv_payload(category_no: str, suffix: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(HEADERS)
    writer.writerow(
        (
            "P001",
            f"批量导入商品 {suffix}",
            "预检后创建",
            category_no,
            "",
            f"IMPORT-{suffix}",
            "标准款",
            '[{"name":"规格","value":"标准"}]',
            12900,
            15900,
            "CNY",
            500,
            "",
            23,
        )
    )
    writer.writerow(
        (
            "P002",
            "错误商品",
            "",
            "cat_missing",
            "",
            f"INVALID-{suffix}",
            "错误款",
            '[{"name":"规格","value":"错误"}]',
            100,
            100,
            "CNY",
            "",
            "",
            0,
        )
    )
    return output.getvalue().encode()


async def test_product_import_precheck_confirm_and_execute(
    client: AsyncClient,
) -> None:
    suffix = secrets.token_hex(5)
    password = f"Admin-Batch-{suffix}-Correct-Horse!"
    security = SecurityService(get_settings())
    storage = MemoryObjectStorage()

    async for session in mysql_session():
        provisioning = await provision_platform_super_admin(
            session,
            security,
            username=f"batch_admin_{suffix}",
            password=password,
        )
        owner = await session.scalar(select(User).where(User.user_no == provisioning.user_no))
        assert owner is not None
        category = Category(
            category_no=new_prefixed_ulid("cat_"),
            category_name=f"导入分类 {suffix}",
            category_code=f"import-{suffix}",
            path="/import",
            level=1,
            category_status="active",
        )
        store = Store(
            store_no=new_prefixed_ulid("sto_"),
            owner_user_id=owner.id,
            store_name=f"导入店铺 {suffix}",
            store_name_normalized=f"import-store-{suffix}",
            store_status="active",
            rating_score=Decimal("0.00"),
            rating_count=0,
            follower_count=0,
            sales_count=0,
            opened_at=utc_now(),
        )
        session.add_all([category, store])
        await session.flush()
        payload = _csv_payload(category.category_no, suffix)
        bucket = "admin-job-artifacts"
        object_key = f"tests/{suffix}/products.csv"
        await storage.put(bucket, object_key, payload, "text/csv")
        source = FileObject(
            file_no=new_prefixed_ulid("file_"),
            bucket=bucket,
            object_key=object_key,
            purpose="admin_import",
            owner_type="store",
            owner_no=store.store_no,
            variant="original",
            declared_mime_type="text/csv",
            detected_mime_type="text/csv",
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).digest(),
            visibility="private",
            sensitivity_level="L1",
            scan_status="safe",
            file_status="active",
            activated_at=utc_now(),
        )
        session.add(source)
        await session.commit()
        store_no = store.store_no
        file_no = source.file_no

    login = await client.post(
        "/api/v1/admin/auth/login",
        json={
            "identifier": f"batch_admin_{suffix}",
            "password": password,
            "client": {"client_type": "web", "device_name": "Batch Job Test"},
        },
    )
    assert login.status_code == 200, login.text
    mfa = await client.post(
        "/api/v1/admin/auth/mfa-verifications",
        headers={"Idempotency-Key": f"batch-mfa-{suffix}"},
        json={
            "challenge_id": login.json()["data"]["challenge_id"],
            "method": "totp",
            "code": pyotp.TOTP(provisioning.totp_secret).now(),
        },
    )
    assert mfa.status_code == 200, mfa.text
    auth = {"Authorization": f"Bearer {mfa.json()['data']['session']['access_token']}"}

    created = await client.post(
        "/api/v1/admin/batch-jobs",
        headers={**auth, "Idempotency-Key": f"batch-create-{suffix}"},
        json={
            "job_type": "product_import",
            "store_id": store_no,
            "input_file_id": file_no,
            "schema_version": "product-import-v1",
        },
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["data"]["job_id"]
    assert created.json()["data"]["status"] == "created"

    async for session in mysql_session():
        assert await BatchJobProcessor(session, storage).process_one() is True

    preview = await client.get(f"/api/v1/admin/batch-jobs/{job_id}", headers=auth)
    assert preview.status_code == 200, preview.text
    assert preview.json()["data"]["status"] == "awaiting_confirmation"
    assert preview.json()["data"]["success_count"] == 1
    assert preview.json()["data"]["failure_count"] == 1
    assert preview.json()["data"]["error_file_id"].startswith("file_")

    items = await client.get(f"/api/v1/admin/batch-jobs/{job_id}/items", headers=auth)
    assert items.status_code == 200, items.text
    assert {item["item_status"] for item in items.json()["data"]["items"]} == {
        "valid",
        "invalid",
    }

    stale = await client.post(
        f"/api/v1/admin/batch-jobs/{job_id}/confirmations",
        headers={
            **auth,
            "If-Match": preview.headers["etag"],
            "Idempotency-Key": f"batch-confirm-stale-{suffix}",
        },
        json={"preview_hash": "0" * 64},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "BATCH_JOB_PREVIEW_CHANGED"

    confirmed = await client.post(
        f"/api/v1/admin/batch-jobs/{job_id}/confirmations",
        headers={
            **auth,
            "If-Match": preview.headers["etag"],
            "Idempotency-Key": f"batch-confirm-{suffix}",
        },
        json={"preview_hash": preview.json()["data"]["preview_hash"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["data"]["status"] == "queued"

    async for session in mysql_session():
        assert await BatchJobProcessor(session, storage).process_one() is True

    completed = await client.get(f"/api/v1/admin/batch-jobs/{job_id}", headers=auth)
    assert completed.status_code == 200, completed.text
    assert completed.json()["data"]["status"] == "partial"
    assert completed.json()["data"]["success_count"] == 1
    assert completed.json()["data"]["failure_count"] == 1

    async for session in mysql_session():
        product = await session.scalar(
            select(Product).where(Product.product_name == f"批量导入商品 {suffix}")
        )
        assert product is not None
        sku = await session.scalar(select(ProductSku).where(ProductSku.product_id == product.id))
        assert sku is not None
        inventory = await session.scalar(select(Inventory).where(Inventory.sku_id == sku.id))
        assert inventory is not None
        assert inventory.on_hand_quantity == 23

    cancellable = await client.post(
        "/api/v1/admin/batch-jobs",
        headers={**auth, "Idempotency-Key": f"batch-create-cancel-{suffix}"},
        json={
            "job_type": "product_import",
            "store_id": store_no,
            "input_file_id": file_no,
            "schema_version": "product-import-v1",
        },
    )
    assert cancellable.status_code == 201, cancellable.text
    cancelled = await client.post(
        f"/api/v1/admin/batch-jobs/{cancellable.json()['data']['job_id']}/cancellations",
        headers={
            **auth,
            "If-Match": cancellable.headers["etag"],
            "Idempotency-Key": f"batch-cancel-{suffix}",
        },
        json={"reason": "集成测试验证执行前取消"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "cancelled"
