from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import utc_now
from app.modules.checkout.models import CheckoutSession, CheckoutSnapshot
from app.modules.identity.models import AuthSession
from app.modules.orders.models import TradeOrder
from app.modules.system.models import IdempotencyRecord


@dataclass(frozen=True)
class LifecycleResult:
    revoked_sessions: int
    expired_checkouts: int
    purged_checkouts: int
    purged_idempotency_records: int

    @property
    def processed(self) -> int:
        return (
            self.revoked_sessions
            + self.expired_checkouts
            + self.purged_checkouts
            + self.purged_idempotency_records
        )


class LifecycleProcessor:
    """Applies bounded, idempotent lifecycle transitions to transient MySQL data."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    async def process_batch(self) -> LifecycleResult:
        now = utc_now()
        limit = self.settings.lifecycle_batch_size

        sessions = list(
            (
                await self.session.scalars(
                    select(AuthSession)
                    .where(AuthSession.revoked_at.is_(None), AuthSession.expires_at <= now)
                    .order_by(AuthSession.expires_at, AuthSession.id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            ).all()
        )
        for session in sessions:
            session.revoked_at = now
            session.revoke_reason = "expired"

        checkouts = list(
            (
                await self.session.scalars(
                    select(CheckoutSession)
                    .where(
                        CheckoutSession.checkout_status == "active",
                        CheckoutSession.expires_at <= now,
                    )
                    .order_by(CheckoutSession.expires_at, CheckoutSession.id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            ).all()
        )
        for checkout in checkouts:
            checkout.checkout_status = "expired"
            checkout.version += 1

        purge_before = now - timedelta(days=self.settings.checkout_retention_days)
        purge_candidates = list(
            (
                await self.session.scalars(
                    select(CheckoutSession.id)
                    .where(
                        CheckoutSession.checkout_status.in_(("expired", "cancelled")),
                        CheckoutSession.updated_at <= purge_before,
                        ~exists(
                            select(TradeOrder.id).where(
                                TradeOrder.checkout_session_id == CheckoutSession.id
                            )
                        ),
                    )
                    .order_by(CheckoutSession.id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            ).all()
        )
        if purge_candidates:
            await self.session.execute(
                delete(CheckoutSnapshot).where(
                    CheckoutSnapshot.checkout_session_id.in_(purge_candidates)
                )
            )
            await self.session.execute(
                delete(CheckoutSession).where(CheckoutSession.id.in_(purge_candidates))
            )

        expired_idempotency = list(
            (
                await self.session.scalars(
                    select(IdempotencyRecord)
                    .where(IdempotencyRecord.expires_at <= now)
                    .order_by(IdempotencyRecord.expires_at, IdempotencyRecord.id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            ).all()
        )
        for record in expired_idempotency:
            await self.session.delete(record)

        await self.session.commit()
        return LifecycleResult(
            revoked_sessions=len(sessions),
            expired_checkouts=len(checkouts),
            purged_checkouts=len(purge_candidates),
            purged_idempotency_records=len(expired_idempotency),
        )
