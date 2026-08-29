from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.modules.agent_runtime.models import UserAgentConsent
from app.modules.identity.models import User


async def require_active_consent(
    session: AsyncSession,
    user: User,
    *,
    consent_type: str,
    scope_type: str,
    scope_no: str | None,
    now: datetime,
) -> UserAgentConsent:
    consent = await session.scalar(
        select(UserAgentConsent)
        .where(
            UserAgentConsent.user_id == user.id,
            UserAgentConsent.consent_type == consent_type,
            UserAgentConsent.scope_type == scope_type,
            UserAgentConsent.scope_no == scope_no,
            UserAgentConsent.consent_status == "active",
            UserAgentConsent.revoked_at.is_(None),
        )
        .order_by(UserAgentConsent.id.desc())
    )
    if consent is None or (consent.expires_at is not None and consent.expires_at <= now):
        raise ApplicationError(
            status=403,
            code="AI_CONSENT_REQUIRED",
            title="AI consent required",
            detail="该智能客服能力需要用户明确授权。",
        )
    return consent
