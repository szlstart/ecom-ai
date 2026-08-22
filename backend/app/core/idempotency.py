from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.core.security import canonical_request_hash, utc_now
from app.modules.system.models import IdempotencyRecord


@dataclass(frozen=True)
class IdempotencyClaim:
    record: IdempotencyRecord
    replayed: bool


class IdempotencyService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def begin(
        self,
        *,
        scope_key: str,
        idempotency_key: str,
        payload: object,
        resource_type: str,
        ttl_days: int = 7,
    ) -> IdempotencyClaim:
        request_hash = canonical_request_hash(payload)
        existing = cast(
            IdempotencyRecord | None,
            await self.session.scalar(
                select(IdempotencyRecord)
                .where(
                    IdempotencyRecord.scope_key == scope_key,
                    IdempotencyRecord.idempotency_key == idempotency_key,
                )
            ),
        )
        if existing is not None:
            return self._existing_claim(existing, request_hash)

        # The initial read cannot serialize two transactions when the row does not yet exist.
        # A savepoint lets the database unique constraint choose the winner without aborting the
        # caller's whole transaction; the loser can then inspect the committed/current row.
        try:
            async with self.session.begin_nested():
                record = IdempotencyRecord(
                    scope_key=scope_key,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    resource_type=resource_type,
                    expires_at=utc_now() + timedelta(days=ttl_days),
                )
                self.session.add(record)
                await self.session.flush()
        except IntegrityError:
            existing = cast(
                IdempotencyRecord | None,
                await self.session.scalar(
                    select(IdempotencyRecord)
                    .where(
                        IdempotencyRecord.scope_key == scope_key,
                        IdempotencyRecord.idempotency_key == idempotency_key,
                    )
                    .with_for_update()
                ),
            )
            if existing is None:
                raise
            claim = self._existing_claim(existing, request_hash)
            # The first non-locking lookup may have opened a REPEATABLE READ snapshot before
            # the winning transaction committed. End that read-only transaction so subsequent
            # resource recovery observes the winner's committed aggregate as well as its key.
            await self.session.commit()
            return claim
        return IdempotencyClaim(record=record, replayed=False)

    @staticmethod
    def _existing_claim(
        existing: IdempotencyRecord, request_hash: bytes
    ) -> IdempotencyClaim:
        if not hmac.compare_digest(existing.request_hash, request_hash):
            raise ApplicationError(
                status=409,
                code="IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD",
                title="Idempotency key reused",
                detail="同一幂等键不能用于不同请求内容。",
            )
        if existing.response_status is None:
            raise ApplicationError(
                status=409,
                code="IDEMPOTENCY_IN_PROGRESS",
                title="Request in progress",
                detail="请求正在处理中，请使用相同幂等键稍后重试。",
                retryable=True,
            )
        return IdempotencyClaim(record=existing, replayed=True)

    @staticmethod
    def complete(
        claim: IdempotencyClaim,
        *,
        response_status: int,
        resource_no: str | None = None,
        response_body: dict[str, object] | None = None,
    ) -> None:
        claim.record.response_status = response_status
        claim.record.resource_no = resource_no
        claim.record.response_body = response_body
