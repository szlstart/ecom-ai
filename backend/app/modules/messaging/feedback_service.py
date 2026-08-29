from __future__ import annotations

from datetime import timedelta
from typing import Literal, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import canonical_request_hash, utc_now
from app.modules.agent_runtime.models import AiFeedback
from app.modules.identity.models import User
from app.modules.messaging.feedback_schemas import AiFeedbackDetailRequest, AiFeedbackView
from app.modules.messaging.models import Conversation, Message


class AiFeedbackService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.idempotency = IdempotencyService(session)

    async def put_reaction(
        self, user: User, conversation_no: str, message_no: str, reaction: str
    ) -> AiFeedbackView:
        _, message = await self._target(user, conversation_no, message_no)
        current = cast(
            AiFeedback | None,
            await self.session.scalar(
                select(AiFeedback)
                .where(
                    AiFeedback.user_id == user.id,
                    AiFeedback.message_id == message.id,
                    AiFeedback.feedback_type.in_(("thumb_up", "thumb_down")),
                    AiFeedback.feedback_status == "submitted",
                )
                .with_for_update()
            ),
        )
        if current is not None and current.feedback_type == reaction:
            return _view(current, message_no)
        now = utc_now()
        if current is not None:
            current.feedback_status = "withdrawn"
            current.withdrawn_at = now
            current.version += 1
            await self.session.flush()
        row = AiFeedback(
            feedback_no=new_prefixed_ulid("fdb_"),
            user_id=user.id,
            conversation_id=message.conversation_id,
            message_id=message.id,
            ai_run_no=message.ai_run_no,
            feedback_type=reaction,
            reason_code=None,
            comment=None,
            content_hash=canonical_request_hash({"reaction": reaction}),
            feedback_status="submitted",
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        result = _view(row, message_no)
        await self.session.commit()
        return result

    async def delete_reaction(
        self, user: User, conversation_no: str, message_no: str
    ) -> AiFeedbackView:
        _, message = await self._target(user, conversation_no, message_no)
        row = cast(
            AiFeedback | None,
            await self.session.scalar(
                select(AiFeedback)
                .where(
                    AiFeedback.user_id == user.id,
                    AiFeedback.message_id == message.id,
                    AiFeedback.feedback_type.in_(("thumb_up", "thumb_down")),
                    AiFeedback.feedback_status == "submitted",
                )
                .with_for_update()
            ),
        )
        if row is None:
            return AiFeedbackView(
                feedback_id=None,
                message_id=message_no,
                feedback_type=None,
                status=None,
                created_at=None,
            )
        row.feedback_status = "withdrawn"
        row.withdrawn_at = utc_now()
        row.version += 1
        await self.session.flush()
        await self.session.refresh(row)
        result = _view(row, message_no)
        await self.session.commit()
        return result

    async def create_detail(
        self,
        user: User,
        conversation_no: str,
        message_no: str,
        feedback_type: str,
        payload: AiFeedbackDetailRequest,
        idempotency_key: str,
    ) -> AiFeedbackView:
        claim = await self.idempotency.begin(
            scope_key=f"ai-feedback:{feedback_type}:{user.id}:{message_no}",
            idempotency_key=idempotency_key,
            payload=payload.model_dump(mode="json"),
            resource_type="ai_feedback",
        )
        _, message = await self._target(user, conversation_no, message_no)
        if claim.replayed and claim.record.resource_no:
            replay = await self.session.scalar(
                select(AiFeedback).where(AiFeedback.feedback_no == claim.record.resource_no)
            )
            if replay is not None:
                return _view(replay, message_no)
        content_hash = canonical_request_hash(payload.model_dump(mode="json"))
        duplicate = await self.session.scalar(
            select(AiFeedback).where(
                AiFeedback.user_id == user.id,
                AiFeedback.message_id == message.id,
                AiFeedback.feedback_type == feedback_type,
                AiFeedback.content_hash == content_hash,
            )
        )
        if duplicate is not None:
            result = _view(duplicate, message_no)
            self.idempotency.complete(
                claim,
                response_status=201,
                resource_no=duplicate.feedback_no,
                response_body=cast(dict[str, object], result.model_dump(mode="json")),
            )
            await self.session.commit()
            return result
        recent = await self.session.scalar(
            select(func.count(AiFeedback.id)).where(
                AiFeedback.user_id == user.id,
                AiFeedback.feedback_type.in_(("report", "correction")),
                AiFeedback.created_at >= utc_now() - timedelta(hours=1),
            )
        )
        if int(recent or 0) >= 10:
            raise ApplicationError(
                status=429,
                code="AI_FEEDBACK_RATE_LIMITED",
                title="Too many feedback requests",
                detail="反馈提交过于频繁，请稍后再试。",
                retryable=True,
            )
        row = AiFeedback(
            feedback_no=new_prefixed_ulid("fdb_"),
            user_id=user.id,
            conversation_id=message.conversation_id,
            message_id=message.id,
            ai_run_no=message.ai_run_no,
            feedback_type=feedback_type,
            reason_code=payload.reason_code,
            comment=payload.comment.strip(),
            content_hash=content_hash,
            feedback_status="submitted",
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        result = _view(row, message_no)
        self.idempotency.complete(
            claim,
            response_status=201,
            resource_no=row.feedback_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def _target(
        self, user: User, conversation_no: str, message_no: str
    ) -> tuple[Conversation, Message]:
        row = (
            await self.session.execute(
                select(Conversation, Message)
                .join(Message, Message.conversation_id == Conversation.id)
                .where(
                    Conversation.conversation_no == conversation_no,
                    Conversation.user_id == user.id,
                    Conversation.deleted_at.is_(None),
                    Message.message_no == message_no,
                    Message.sender_type == "agent",
                    Message.message_status != "hidden",
                    Message.recalled_at.is_(None),
                )
            )
        ).one_or_none()
        if row is None:
            raise ApplicationError(
                status=404,
                code="AI_FEEDBACK_TARGET_NOT_FOUND",
                title="Feedback target not found",
                detail="该消息不存在或不可反馈。",
            )
        return cast(tuple[Conversation, Message], row)


def _view(row: AiFeedback, message_no: str) -> AiFeedbackView:
    return AiFeedbackView(
        feedback_id=row.feedback_no,
        message_id=message_no,
        feedback_type=cast(
            Literal["thumb_up", "thumb_down", "report", "correction"], row.feedback_type
        ),
        status=cast(
            Literal["submitted", "withdrawn", "reviewed", "resolved", "dismissed"],
            row.feedback_status,
        ),
        created_at=row.created_at,
    )
