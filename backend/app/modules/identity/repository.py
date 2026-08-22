from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.content.models import PlatformContentEntry, PlatformContentVersion
from app.modules.identity.models import (
    AuthSession,
    PasswordResetRecord,
    User,
    UserAddress,
    UserCredential,
    VerificationCode,
)
from app.modules.rbac.models import Role, UserRole
from app.modules.system.models import IdempotencyRecord


class IdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def user_by_username(self, normalized: str, *, for_update: bool = False) -> User | None:
        statement = select(User).where(User.username_normalized == normalized)
        return cast(User | None, await self.session.scalar(_locked(statement, for_update)))

    async def user_by_no(self, user_no: str, *, for_update: bool = False) -> User | None:
        statement = select(User).where(User.user_no == user_no)
        return cast(User | None, await self.session.scalar(_locked(statement, for_update)))

    async def credential_by_identifier(
        self, credential_type: str, identifier_hash: bytes, *, for_update: bool = False
    ) -> UserCredential | None:
        statement = select(UserCredential).where(
            UserCredential.credential_type == credential_type,
            UserCredential.identifier_hash == identifier_hash,
            UserCredential.credential_status == "active",
        )
        return cast(
            UserCredential | None,
            await self.session.scalar(_locked(statement, for_update)),
        )

    async def password_credential(
        self, user_id: int, *, for_update: bool = False
    ) -> UserCredential | None:
        statement = select(UserCredential).where(
            UserCredential.user_id == user_id,
            UserCredential.credential_type == "password",
            UserCredential.credential_status == "active",
        )
        return cast(
            UserCredential | None,
            await self.session.scalar(_locked(statement, for_update)),
        )

    async def credentials_for_user(self, user_id: int) -> list[UserCredential]:
        return list(
            (
                await self.session.scalars(
                    select(UserCredential).where(
                        UserCredential.user_id == user_id,
                        UserCredential.credential_status == "active",
                    )
                )
            ).all()
        )

    async def verification_by_no(
        self, verification_no: str, *, for_update: bool = False
    ) -> VerificationCode | None:
        statement = select(VerificationCode).where(
            VerificationCode.verification_no == verification_no
        )
        return cast(
            VerificationCode | None,
            await self.session.scalar(_locked(statement, for_update)),
        )

    async def active_legal_versions(
        self, locale: str = "zh-CN", region_code: str = "CN"
    ) -> list[tuple[PlatformContentEntry, PlatformContentVersion]]:
        now = datetime.now(UTC).replace(tzinfo=None)
        result = await self.session.execute(
            select(PlatformContentEntry, PlatformContentVersion)
            .join(
                PlatformContentVersion,
                PlatformContentVersion.entry_id == PlatformContentEntry.id,
            )
            .where(
                PlatformContentEntry.content_type == "legal_document",
                PlatformContentEntry.content_status == "active",
                PlatformContentVersion.locale == locale,
                PlatformContentVersion.region_code == region_code,
                PlatformContentVersion.publish_status == "published",
                PlatformContentVersion.effective_at <= now,
                or_(
                    PlatformContentVersion.expires_at.is_(None),
                    PlatformContentVersion.expires_at > now,
                ),
            )
        )
        return [(row[0], row[1]) for row in result.all()]

    async def legal_version(
        self, document_type: str, document_version: str
    ) -> tuple[PlatformContentEntry, PlatformContentVersion] | None:
        result = await self.session.execute(
            select(PlatformContentEntry, PlatformContentVersion)
            .join(
                PlatformContentVersion,
                PlatformContentVersion.entry_id == PlatformContentEntry.id,
            )
            .where(
                PlatformContentEntry.content_key == f"legal.{document_type}",
                PlatformContentVersion.document_version == document_version,
                PlatformContentVersion.publish_status == "published",
            )
        )
        row = result.one_or_none()
        return None if row is None else (row[0], row[1])

    async def role_by_code(self, role_code: str) -> Role | None:
        return cast(
            Role | None,
            await self.session.scalar(
                select(Role).where(Role.role_code == role_code, Role.role_status == "active")
            ),
        )

    async def active_role_grants(self, user_id: int) -> list[UserRole]:
        now = datetime.now(UTC).replace(tzinfo=None)
        return list(
            (
                await self.session.scalars(
                    select(UserRole).where(
                        UserRole.user_id == user_id,
                        UserRole.grant_status == "active",
                        or_(UserRole.expires_at.is_(None), UserRole.expires_at > now),
                    )
                )
            ).all()
        )

    async def session_by_refresh_hash(
        self, token_hash: bytes, *, for_update: bool = False
    ) -> AuthSession | None:
        statement = select(AuthSession).where(AuthSession.refresh_token_hash == token_hash)
        return cast(AuthSession | None, await self.session.scalar(_locked(statement, for_update)))

    async def session_by_no(
        self, user_id: int, session_no: str, *, for_update: bool = False
    ) -> AuthSession | None:
        statement = select(AuthSession).where(
            AuthSession.user_id == user_id,
            AuthSession.session_no == session_no,
        )
        return cast(AuthSession | None, await self.session.scalar(_locked(statement, for_update)))

    async def active_sessions(self, user_id: int, audience: str) -> list[AuthSession]:
        now = datetime.now(UTC).replace(tzinfo=None)
        return list(
            (
                await self.session.scalars(
                    select(AuthSession)
                    .where(
                        AuthSession.user_id == user_id,
                        AuthSession.audience == audience,
                        AuthSession.revoked_at.is_(None),
                        AuthSession.expires_at > now,
                    )
                    .order_by(AuthSession.last_seen_at.desc(), AuthSession.id.desc())
                )
            ).all()
        )

    async def revoke_family(self, token_family_no: str, now: datetime, reason: str) -> None:
        await self.session.execute(
            update(AuthSession)
            .where(
                AuthSession.token_family_no == token_family_no,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=now, revoke_reason=reason)
        )

    async def revoke_user_sessions(
        self,
        user_id: int,
        now: datetime,
        reason: str,
        *,
        except_session_id: int | None = None,
        audience: str | None = None,
    ) -> None:
        statement = update(AuthSession).where(
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
        )
        if except_session_id is not None:
            statement = statement.where(AuthSession.id != except_session_id)
        if audience is not None:
            statement = statement.where(AuthSession.audience == audience)
        await self.session.execute(statement.values(revoked_at=now, revoke_reason=reason))

    async def reset_by_hash(
        self, token_hash: bytes, *, for_update: bool = False
    ) -> PasswordResetRecord | None:
        statement = select(PasswordResetRecord).where(
            PasswordResetRecord.reset_token_hash == token_hash
        )
        return cast(
            PasswordResetRecord | None,
            await self.session.scalar(_locked(statement, for_update)),
        )

    async def idempotency_record(
        self, scope_key: str, key: str, *, for_update: bool = False
    ) -> IdempotencyRecord | None:
        statement = select(IdempotencyRecord).where(
            IdempotencyRecord.scope_key == scope_key,
            IdempotencyRecord.idempotency_key == key,
        )
        return cast(
            IdempotencyRecord | None,
            await self.session.scalar(_locked(statement, for_update)),
        )

    async def addresses(self, user_id: int) -> list[UserAddress]:
        return list(
            (
                await self.session.scalars(
                    select(UserAddress)
                    .where(UserAddress.user_id == user_id, UserAddress.deleted_at.is_(None))
                    .order_by(UserAddress.is_default.desc(), UserAddress.id.desc())
                )
            ).all()
        )

    async def address_by_no(
        self, user_id: int, address_no: str, *, for_update: bool = False
    ) -> UserAddress | None:
        statement = select(UserAddress).where(
            UserAddress.user_id == user_id,
            UserAddress.address_no == address_no,
            UserAddress.deleted_at.is_(None),
        )
        return cast(
            UserAddress | None,
            await self.session.scalar(_locked(statement, for_update)),
        )

    async def address_count(self, user_id: int) -> int:
        return int(
            await self.session.scalar(
                select(func.count(UserAddress.id)).where(
                    UserAddress.user_id == user_id,
                    UserAddress.deleted_at.is_(None),
                )
            )
            or 0
        )


def _locked[SelectRow: tuple[Any, ...]](
    statement: Select[SelectRow], enabled: bool
) -> Select[SelectRow]:
    return statement.with_for_update() if enabled else statement
