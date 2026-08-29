from collections.abc import Sequence
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.system.models import DeadLetterEvent, OutboxEvent


class DeadLetterRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(
        self,
        *,
        scopes: Sequence[tuple[str, int]],
        status: str | None,
        event_type: str | None,
        limit: int,
    ) -> list[DeadLetterEvent]:
        statement = select(DeadLetterEvent)
        if ("platform", 0) not in scopes:
            allowed = [
                (scope_type, scope_id) for scope_type, scope_id in scopes if scope_type == "store"
            ]
            if not allowed:
                return []
            statement = statement.where(
                DeadLetterEvent.scope_type == "store",
                DeadLetterEvent.scope_id.in_([scope_id for _, scope_id in allowed]),
            )
        if status:
            statement = statement.where(DeadLetterEvent.dead_status == status)
        if event_type:
            statement = statement.where(DeadLetterEvent.event_type == event_type)
        return list(
            (
                await self.session.scalars(
                    statement.order_by(
                        DeadLetterEvent.last_failed_at.desc(), DeadLetterEvent.id.desc()
                    ).limit(limit)
                )
            ).all()
        )

    async def by_no(
        self, dead_letter_no: str, *, for_update: bool = False
    ) -> DeadLetterEvent | None:
        statement = select(DeadLetterEvent).where(DeadLetterEvent.dead_letter_no == dead_letter_no)
        if for_update:
            statement = statement.with_for_update()
        return cast(DeadLetterEvent | None, await self.session.scalar(statement))

    async def outbox_by_no(self, event_no: str, *, for_update: bool = False) -> OutboxEvent | None:
        statement = select(OutboxEvent).where(OutboxEvent.event_no == event_no)
        if for_update:
            statement = statement.with_for_update()
        return cast(OutboxEvent | None, await self.session.scalar(statement))
