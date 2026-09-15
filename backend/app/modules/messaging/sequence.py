from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.messaging.models import Conversation


async def lock_conversation_for_append(
    session: AsyncSession,
    conversation_id: int,
) -> Conversation:
    """Lock and refresh the sequence cursor immediately before appending."""

    conversation = await session.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        raise RuntimeError("conversation disappeared before message append")
    return conversation
