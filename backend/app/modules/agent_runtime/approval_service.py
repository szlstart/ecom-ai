from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ApplicationError
from app.core.id_generator import new_prefixed_ulid
from app.core.idempotency import IdempotencyService
from app.core.security import SecurityService, canonical_request_hash, utc_now
from app.modules.after_sale.schemas import (
    RefundApplicationCreateRequest,
    RefundEligibilityItemRequest,
    RefundEligibilityRequest,
)
from app.modules.after_sale.service import AfterSaleService
from app.modules.agent_runtime.consent import require_active_consent
from app.modules.agent_runtime.exclusive_context import TrustedExclusiveAgentContext
from app.modules.agent_runtime.models import (
    AgentRefundDraft,
    AgentRun,
    AgentToolAction,
    AgentToolApproval,
)
from app.modules.agent_runtime.schemas import AgentApprovalDecisionRequest, AgentApprovalView
from app.modules.identity.models import User
from app.modules.messaging.models import Conversation, Message
from app.modules.orders.models import Order, OrderItem
from app.modules.stores.models import Store
from app.modules.system.models import IdempotencyRecord, OutboxEvent


class AgentApprovalService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        security: SecurityService,
    ) -> None:
        self.session = session
        self.settings = settings
        self.security = security
        self.after_sale = AfterSaleService(session, settings, security)
        self.idempotency = IdempotencyService(session)

    async def reconcile_unknown(self, *, limit: int = 50) -> int:
        """Resolve locally unknown writes from their durable idempotency record.

        A completed record proves the refund exists. A missing record after the grace
        period proves no refund transaction committed and safely requeues the same
        approved action with the same idempotency key. A pending record remains
        untouched because its outcome cannot yet be established.
        """
        now = utc_now()
        cutoff = now - timedelta(seconds=30)
        actions = list(
            (
                await self.session.scalars(
                    select(AgentToolAction)
                    .where(
                        AgentToolAction.action_status.in_({"running", "outcome_unknown"}),
                        AgentToolAction.updated_at <= cutoff,
                    )
                    .order_by(AgentToolAction.updated_at, AgentToolAction.id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            ).all()
        )
        reconciled = 0
        for action in actions:
            approval = await self.session.get(
                AgentToolApproval, action.approval_id, with_for_update=True
            )
            draft = (
                await self.session.get(AgentRefundDraft, approval.draft_id, with_for_update=True)
                if approval is not None
                else None
            )
            run = await self.session.get(AgentRun, action.run_id, with_for_update=True)
            conversation = (
                await self.session.get(Conversation, run.conversation_id)
                if run is not None
                else None
            )
            user = (
                await self.session.get(User, conversation.user_id)
                if conversation is not None
                else None
            )
            if approval is None or draft is None or run is None or user is None:
                action.action_status = "failed"
                action.error_code = "AGENT_RECONCILIATION_SCOPE_MISSING"
                action.finished_at = now
                action.version += 1
                reconciled += 1
                continue
            record = await self.session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.scope_key == f"refund:create:{user.user_no}",
                    IdempotencyRecord.idempotency_key == action.idempotency_key,
                )
            )
            if record is not None and record.response_status is None:
                continue
            if record is not None and record.resource_no:
                action.action_status = "succeeded"
                action.resource_no = record.resource_no
                action.error_code = None
                action.finished_at = now
                action.version += 1
                approval.approval_status = "consumed"
                approval.consumed_at = now
                approval.version += 1
                draft.draft_status = "consumed"
                draft.consumed_at = now
                draft.version += 1
                run.run_status = "queued"
                run.current_phase = "approval_reconciled"
                run.error_code = None
                run.version += 1
                reconciled += 1
                continue
            if record is None:
                run.run_status = "queued"
                run.current_phase = "approval_retry_queued"
                run.error_code = None
                run.version += 1
                action.version += 1
                reconciled += 1
                continue
            action.action_status = "failed"
            action.error_code = "AGENT_RECONCILIATION_RESULT_INVALID"
            action.finished_at = now
            action.version += 1
            run.run_status = "completed"
            run.current_phase = "completed"
            run.error_code = action.error_code
            run.version += 1
            reconciled += 1
        return reconciled

    async def build_refund_draft(
        self,
        context: TrustedExclusiveAgentContext,
        order_no: str,
        user_text: str,
    ) -> dict[str, object]:
        await require_active_consent(
            self.session,
            context.user,
            consent_type="after_sale_write",
            scope_type="user",
            scope_no=None,
            now=utc_now(),
        )
        row = (
            await self.session.execute(
                select(Order, Store)
                .join(Store, Store.id == Order.store_id)
                .where(Order.order_no == order_no, Order.user_id == context.user.id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise _not_accessible()
        order, store = row
        items = list(
            (
                await self.session.scalars(
                    select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)
                )
            ).all()
        )
        candidates = [
            item
            for item in items
            if item.refunded_quantity < item.quantity and item.refunded_amount < item.payable_amount
        ]
        if not candidates:
            raise ApplicationError(
                status=409,
                code="REFUND_ITEM_CAPACITY_CHANGED",
                title="Refund unavailable",
                detail="当前订单没有可申请退款的商品数量。",
            )
        candidate = _select_refund_candidate(candidates, user_text)
        quantity = _requested_quantity(user_text)
        available_quantity = candidate.quantity - candidate.refunded_quantity
        if quantity > available_quantity:
            raise ApplicationError(
                status=409,
                code="REFUND_ITEM_QUANTITY_UNAVAILABLE",
                title="Refund quantity unavailable",
                detail=f"该商品当前最多可申请 {available_quantity} 件，请重新说明数量。",
            )
        refund_type = "return_and_refund" if "退货" in user_text else "refund_only"
        reason_code = _reason_code(user_text)
        eligibility = await self.after_sale.eligibility(
            context.user,
            RefundEligibilityRequest(
                order_id=order.order_no,
                items=[
                    RefundEligibilityItemRequest(
                        order_item_id=candidate.order_item_no,
                        quantity=quantity,
                    )
                ],
                requested_type=refund_type,
                reason_code=reason_code,
            ),
        )
        if not eligibility.eligible or eligibility.eligibility_token is None:
            raise ApplicationError(
                status=409,
                code=(
                    eligibility.blocking_reasons[0]
                    if eligibility.blocking_reasons
                    else "REFUND_NOT_ELIGIBLE"
                ),
                title="Refund not eligible",
                detail="当前订单商品不符合自动退款申请条件。",
            )
        detail = user_text.strip()[:500]
        draft_payload: dict[str, object] = {
            "order_id": order.order_no,
            "items": [{"order_item_id": candidate.order_item_no, "quantity": quantity}],
            "refund_type": refund_type,
            "reason_code": reason_code,
            "reason_detail": detail,
            "evidence_file_ids": [],
            "requested_amount": eligibility.suggested_refund_amount.model_dump(mode="json"),
            "policy_accepted": True,
            "policy_version": str(order.policy_snapshot.get("version", "refund-policy-v1")),
        }
        arguments_hash = canonical_request_hash(draft_payload)
        draft_no = new_prefixed_ulid("rfd_")
        now = utc_now()
        expires_at = min(eligibility.expires_at, now + timedelta(minutes=10))
        draft = AgentRefundDraft(
            draft_no=draft_no,
            run_id=context.run.id,
            user_id=context.user.id,
            order_id=order.id,
            draft_payload=draft_payload,
            eligibility_token_ciphertext=self.security.encrypt(
                f"agent-refund-draft:{draft_no}", eligibility.eligibility_token
            ),
            arguments_hash=arguments_hash,
            draft_status="active",
            expires_at=expires_at,
        )
        self.session.add(draft)
        await self.session.flush()
        approval = AgentToolApproval(
            approval_no=new_prefixed_ulid("apr_"),
            run_id=context.run.id,
            user_id=context.user.id,
            conversation_id=context.conversation.id,
            draft_id=draft.id,
            action_type="refund_submit",
            arguments_hash=arguments_hash,
            resource_versions={
                "order": order.version,
                "order_item": candidate.version,
                "agent_version": context.agent_version.version_no,
            },
            approval_status="pending",
            decision=None,
            expires_at=expires_at,
        )
        self.session.add(approval)
        await self.session.flush()
        context.run.run_status = "waiting"
        context.run.current_phase = "waiting_confirmation"
        context.run.version += 1
        await self._approval_message(
            context,
            approval,
            draft,
            store,
            candidate,
            eligibility.suggested_refund_amount.model_dump(mode="json"),
        )
        return {
            "approval_id": approval.approval_no,
            "draft_id": draft.draft_no,
            "expires_at": expires_at,
        }

    async def get(self, user: User, approval_no: str) -> AgentApprovalView:
        row = await self._owned(user, approval_no)
        if row is None:
            raise _not_accessible()
        approval, draft, run, conversation = row
        await self._expire_if_needed(approval, draft, run)
        return _approval_view(approval, draft, run, conversation)

    async def decide(
        self,
        user: User,
        approval_no: str,
        payload: AgentApprovalDecisionRequest,
        expected_version: int,
        idempotency_key: str,
    ) -> AgentApprovalView:
        claim = await self.idempotency.begin(
            scope_key=f"agent-approval:{user.user_no}:{approval_no}",
            idempotency_key=idempotency_key,
            payload={"decision": payload.decision, "expected_version": expected_version},
            resource_type="agent_approval",
        )
        row = await self._owned(user, approval_no, for_update=True)
        if row is None:
            raise _not_accessible()
        approval, draft, run, conversation = row
        if claim.replayed:
            return _approval_view(approval, draft, run, conversation)
        await self._expire_if_needed(approval, draft, run)
        if approval.approval_status != "pending":
            raise ApplicationError(
                status=409,
                code="AGENT_APPROVAL_NOT_PENDING",
                title="Approval not pending",
                detail="该确认已经处理或过期。",
            )
        if approval.version != expected_version:
            raise ApplicationError(
                status=412,
                code="RESOURCE_VERSION_CONFLICT",
                title="Version conflict",
                detail="确认卡片已经变化，请刷新后重试。",
            )
        now = utc_now()
        approval.approval_status = "approved" if payload.decision == "approve" else "rejected"
        approval.decision = payload.decision
        approval.decided_at = now
        approval.version += 1
        run.run_status = "queued"
        run.current_phase = "approval_decided"
        run.version += 1
        result = _approval_view(approval, draft, run, conversation)
        self.idempotency.complete(
            claim,
            response_status=200,
            resource_no=approval.approval_no,
            response_body=cast(dict[str, object], result.model_dump(mode="json")),
        )
        await self.session.commit()
        return result

    async def execute_approved(
        self, context: TrustedExclusiveAgentContext
    ) -> tuple[str, str | None, str | None]:
        approval = await self.session.scalar(
            select(AgentToolApproval)
            .where(
                AgentToolApproval.run_id == context.run.id,
                AgentToolApproval.action_type == "refund_submit",
            )
            .with_for_update()
        )
        if approval is None:
            raise ApplicationError(
                status=409,
                code="AGENT_APPROVAL_REQUIRED",
                title="Approval required",
                detail="缺少退款提交确认。",
            )
        draft = await self.session.get(AgentRefundDraft, approval.draft_id, with_for_update=True)
        if draft is None or draft.user_id != context.user.id:
            raise _not_accessible()
        if approval.approval_status == "rejected":
            draft.draft_status = "invalidated"
            draft.version += 1
            return "rejected", None, None
        if approval.approval_status not in {"approved", "consumed"}:
            raise ApplicationError(
                status=409,
                code="AGENT_APPROVAL_NOT_APPROVED",
                title="Approval not approved",
                detail="退款提交尚未获得有效确认。",
            )
        if approval.expires_at <= utc_now() or draft.expires_at <= utc_now():
            approval.approval_status = "expired"
            draft.draft_status = "expired"
            approval.version += 1
            draft.version += 1
            return "expired", None, "AGENT_APPROVAL_EXPIRED"
        if (
            approval.arguments_hash != draft.arguments_hash
            or canonical_request_hash(draft.draft_payload) != draft.arguments_hash
        ):
            raise ApplicationError(
                status=409,
                code="AGENT_APPROVAL_ARGUMENTS_MISMATCH",
                title="Approval arguments mismatch",
                detail="确认参数校验失败，未执行退款提交。",
            )
        await self._validate_resource_versions(context, approval, draft)
        await require_active_consent(
            self.session,
            context.user,
            consent_type="after_sale_write",
            scope_type="user",
            scope_no=None,
            now=utc_now(),
        )
        approval_id = approval.id
        draft_id = draft.id
        eligibility_token = self.security.decrypt(
            f"agent-refund-draft:{draft.draft_no}", draft.eligibility_token_ciphertext
        )
        execution_payload = dict(draft.draft_payload)
        for display_only_field in ("order_id", "evidence_file_ids", "policy_version"):
            execution_payload.pop(display_only_field, None)
        try:
            create_payload = RefundApplicationCreateRequest.model_validate(
                {**execution_payload, "eligibility_token": eligibility_token}
            )
        except ValueError as exc:
            raise ApplicationError(
                status=409,
                code="AGENT_APPROVAL_DRAFT_INVALID",
                title="Approval draft invalid",
                detail="退款申请草稿不再有效，未执行提交。",
            ) from exc
        action = await self.session.scalar(
            select(AgentToolAction).where(AgentToolAction.approval_id == approval.id)
        )
        if action is not None and action.action_status == "succeeded":
            return "succeeded", action.resource_no, None
        if action is None:
            action = AgentToolAction(
                action_no=new_prefixed_ulid("act_"),
                approval_id=approval.id,
                run_id=context.run.id,
                action_type="refund_submit",
                arguments_hash=draft.arguments_hash,
                idempotency_key=f"agent-action-{approval.approval_no}",
                action_status="running",
                started_at=utc_now(),
            )
            self.session.add(action)
        else:
            action.action_status = "running"
            action.error_code = None
            action.started_at = utc_now()
            action.version += 1
        action_idempotency_key = action.idempotency_key
        await self.session.commit()
        await self.session.refresh(context.user)
        try:
            refund = await self.after_sale.create(
                context.user,
                create_payload,
                action_idempotency_key,
            )
        except TimeoutError:
            await self.session.rollback()
            action = await self.session.scalar(
                select(AgentToolAction)
                .where(AgentToolAction.approval_id == approval_id)
                .with_for_update()
            )
            assert action is not None
            action.action_status = "outcome_unknown"
            action.error_code = "TOOL_TIMEOUT_UNKNOWN"
            action.version += 1
            await self.session.commit()
            return "outcome_unknown", None, action.error_code
        except ApplicationError as exc:
            await self.session.rollback()
            action = await self.session.scalar(
                select(AgentToolAction)
                .where(AgentToolAction.approval_id == approval_id)
                .with_for_update()
            )
            approval = await self.session.get(AgentToolApproval, approval_id, with_for_update=True)
            draft = await self.session.get(AgentRefundDraft, draft_id, with_for_update=True)
            assert action is not None and approval is not None and draft is not None
            action.action_status = "failed"
            action.error_code = exc.code
            action.finished_at = utc_now()
            action.version += 1
            approval.approval_status = "consumed"
            approval.consumed_at = utc_now()
            approval.version += 1
            draft.draft_status = "invalidated"
            draft.version += 1
            await self.session.commit()
            return "failed", None, exc.code
        except Exception:
            await self.session.rollback()
            action = await self.session.scalar(
                select(AgentToolAction)
                .where(AgentToolAction.approval_id == approval_id)
                .with_for_update()
            )
            assert action is not None
            action.action_status = "outcome_unknown"
            action.error_code = "TOOL_OUTCOME_UNKNOWN"
            action.version += 1
            await self.session.commit()
            return "outcome_unknown", None, action.error_code
        action = await self.session.scalar(
            select(AgentToolAction)
            .where(AgentToolAction.approval_id == approval_id)
            .with_for_update()
        )
        approval = await self.session.get(AgentToolApproval, approval_id, with_for_update=True)
        draft = await self.session.get(AgentRefundDraft, draft_id, with_for_update=True)
        assert action is not None and approval is not None and draft is not None
        action.action_status = "succeeded"
        action.resource_no = refund.refund_id
        action.finished_at = utc_now()
        action.version += 1
        approval.approval_status = "consumed"
        approval.consumed_at = utc_now()
        approval.version += 1
        draft.draft_status = "consumed"
        draft.consumed_at = utc_now()
        draft.version += 1
        await self.session.commit()
        return "succeeded", refund.refund_id, None

    async def _validate_resource_versions(
        self,
        context: TrustedExclusiveAgentContext,
        approval: AgentToolApproval,
        draft: AgentRefundDraft,
    ) -> None:
        item_payload = draft.draft_payload.get("items")
        item_no = (
            item_payload[0].get("order_item_id")
            if isinstance(item_payload, list) and item_payload and isinstance(item_payload[0], dict)
            else None
        )
        order = await self.session.get(Order, draft.order_id, with_for_update=True)
        item = (
            await self.session.scalar(
                select(OrderItem)
                .where(OrderItem.order_id == draft.order_id, OrderItem.order_item_no == item_no)
                .with_for_update()
            )
            if isinstance(item_no, str)
            else None
        )
        versions = approval.resource_versions
        if (
            order is None
            or order.user_id != context.user.id
            or item is None
            or order.version != versions.get("order")
            or item.version != versions.get("order_item")
            or context.agent_version.version_no != versions.get("agent_version")
        ):
            raise ApplicationError(
                status=409,
                code="AGENT_APPROVAL_RESOURCE_CHANGED",
                title="Approved resource changed",
                detail="订单、商品或 Agent 版本已变化，原确认已失效，未执行退款提交。",
            )

    async def _approval_message(
        self,
        context: TrustedExclusiveAgentContext,
        approval: AgentToolApproval,
        draft: AgentRefundDraft,
        store: Store,
        item: OrderItem,
        amount: dict[str, Any],
    ) -> None:
        now = utc_now()
        conversation = context.conversation
        conversation.last_sequence_no += 1
        conversation.last_message_at = now
        conversation.version += 1
        message = Message(
            message_no=new_prefixed_ulid("msg_"),
            conversation_id=conversation.id,
            sequence_no=conversation.last_sequence_no,
            client_message_no=None,
            sender_type="agent",
            sender_id=None,
            message_type="refund_approval",
            text_content="退款申请草稿已准备好。请核对卡片，只有点击确认后才会提交。",
            content_payload={
                "run_id": context.run.run_no,
                "approval_id": approval.approval_no,
                "approval_version": approval.version,
                "draft_id": draft.draft_no,
                "order_id": cast(str, draft.draft_payload["order_id"]),
                "store_name": store.store_name,
                "order_item_id": item.order_item_no,
                "product_name": item.product_name,
                "sku_name": item.sku_name,
                "quantity": cast(list[dict[str, object]], draft.draft_payload["items"])[0][
                    "quantity"
                ],
                "refund_type": draft.draft_payload["refund_type"],
                "reason_detail": draft.draft_payload["reason_detail"],
                "evidence_file_ids": draft.draft_payload["evidence_file_ids"],
                "requested_amount": amount,
                "policy_version": draft.draft_payload["policy_version"],
                "expires_at": approval.expires_at.isoformat() + "Z",
                "requires_explicit_confirmation": True,
            },
            message_status="sent",
            moderation_status="passed",
            sent_at=now,
        )
        self.session.add(message)
        await self.session.flush()
        conversation.last_message_id = message.id
        self.session.add(
            OutboxEvent(
                event_no=new_prefixed_ulid("evt_"),
                event_type="message.sent.v1",
                aggregate_type="conversation",
                aggregate_no=conversation.conversation_no,
                aggregate_version=conversation.version,
                payload={
                    "conversation_id": conversation.conversation_no,
                    "message_id": message.message_no,
                },
                event_status="pending",
                available_at=now,
                attempt_count=0,
                trace_id=context.run.trace_id,
            )
        )

    async def _owned(
        self, user: User, approval_no: str, *, for_update: bool = False
    ) -> tuple[AgentToolApproval, AgentRefundDraft, AgentRun, Conversation] | None:
        statement = (
            select(AgentToolApproval, AgentRefundDraft, AgentRun, Conversation)
            .join(AgentRefundDraft, AgentRefundDraft.id == AgentToolApproval.draft_id)
            .join(AgentRun, AgentRun.id == AgentToolApproval.run_id)
            .join(Conversation, Conversation.id == AgentToolApproval.conversation_id)
            .where(
                AgentToolApproval.approval_no == approval_no,
                AgentToolApproval.user_id == user.id,
                AgentRefundDraft.user_id == user.id,
                Conversation.user_id == user.id,
                Conversation.conversation_type == "exclusive",
            )
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self.session.execute(statement)).one_or_none()
        return cast(
            tuple[AgentToolApproval, AgentRefundDraft, AgentRun, Conversation] | None,
            row,
        )

    async def _expire_if_needed(
        self, approval: AgentToolApproval, draft: AgentRefundDraft, run: AgentRun
    ) -> None:
        if approval.approval_status == "pending" and approval.expires_at <= utc_now():
            approval.approval_status = "expired"
            approval.version += 1
            draft.draft_status = "expired"
            draft.version += 1
            run.run_status = "queued"
            run.current_phase = "approval_expired"
            run.version += 1
            await self.session.commit()


def _select_refund_candidate(candidates: list[OrderItem], user_text: str) -> OrderItem:
    if len(candidates) == 1:
        return candidates[0]
    normalized_text = re.sub(r"\s+", "", user_text).casefold()
    matches = [
        item
        for item in candidates
        if any(
            value and re.sub(r"\s+", "", value).casefold() in normalized_text
            for value in (item.product_name, item.sku_name)
        )
    ]
    if len(matches) == 1:
        return matches[0]
    raise ApplicationError(
        status=409,
        code="AGENT_REFUND_ITEM_SELECTION_REQUIRED",
        title="Refund item selection required",
        detail="该订单有多个可售后商品，请明确说明商品名称，或前往订单详情页选择商品。",
    )


def _requested_quantity(user_text: str) -> int:
    match = re.search(r"(?<!\d)(\d{1,6})\s*(?:件|个|套|台)", user_text)
    return int(match.group(1)) if match is not None else 1


def _reason_code(user_text: str) -> str:
    if any(term in user_text for term in ("破损", "损坏", "坏了", "故障")):
        return "DAMAGED"
    if any(term in user_text for term in ("描述不符", "不一致", "货不对版")):
        return "NOT_AS_DESCRIBED"
    if any(term in user_text for term in ("发错", "错发", "不是我买")):
        return "WRONG_ITEM"
    if any(term in user_text for term in ("不想要", "不需要", "买错")):
        return "NO_LONGER_NEEDED"
    return "OTHER"


def _approval_view(
    approval: AgentToolApproval,
    draft: AgentRefundDraft,
    run: AgentRun,
    conversation: Conversation,
) -> AgentApprovalView:
    return AgentApprovalView(
        approval_id=approval.approval_no,
        run_id=run.run_no,
        conversation_id=conversation.conversation_no,
        action_type="refund_submit",
        approval_status=cast(Any, approval.approval_status),
        decision=cast(Any, approval.decision),
        draft=draft.draft_payload,
        expires_at=approval.expires_at,
        decided_at=approval.decided_at,
        version=approval.version,
    )


def _not_accessible() -> ApplicationError:
    return ApplicationError(
        status=404,
        code="RESOURCE_NOT_FOUND",
        title="Resource not found",
        detail="确认资源不存在或不可访问。",
    )
