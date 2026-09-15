from datetime import datetime
from typing import Literal

from pydantic import Field

from app.api.schemas import StrictRequest

AgentRunStatus = Literal["queued", "running", "waiting", "completed", "failed", "cancelled"]
AgentConsentStatus = Literal["active", "paused", "revoked"]
AgentApprovalActionType = Literal[
    "refund_submit",
    "cart_clear",
    "merchant_store_status",
    "merchant_store_profile",
    "merchant_store_email_update",
    "merchant_store_logo_update",
    "merchant_inventory_set",
    "merchant_price_set",
    "merchant_product_status",
    "merchant_product_delete",
    "merchant_product_submit",
    "merchant_product_image_description",
    "merchant_product_fulfillment",
    "merchant_product_draft_create",
    "merchant_product_profile",
    "merchant_product_sku_create",
    "merchant_product_sku_update",
    "merchant_product_sku_disable",
    "merchant_product_sku_image_replace",
    "merchant_product_faq_upsert",
    "merchant_product_faq_delete",
    "merchant_product_detail_section_upsert",
    "merchant_product_detail_section_delete",
    "merchant_store_policy_manage",
    "merchant_refund_decision",
    "merchant_refund_more_info",
    "merchant_support_claim",
    "merchant_support_reply",
    "merchant_support_resolve",
    "merchant_shipment_create",
    "merchant_shipment_progress",
    "merchant_review_reply",
    "admin_user_status",
    "admin_user_force_logout",
    "admin_user_create",
    "admin_user_password_reset_requirement",
    "admin_user_wallet_adjust",
    "admin_user_profile",
    "admin_user_avatar_update",
    "admin_user_delete",
    "admin_user_address_create",
    "admin_user_address_update",
    "admin_user_address_delete",
    "admin_user_address_set_default",
    "admin_user_cart_item_update",
    "admin_user_cart_item_delete",
    "admin_user_cart_clear",
    "admin_user_favorite_product_remove",
    "admin_user_favorite_store_remove",
    "admin_store_status",
    "admin_store_create",
    "admin_store_profile",
    "admin_store_merchant_email_update",
    "admin_store_logo_update",
    "admin_store_delete",
    "admin_product_status",
    "admin_product_delete",
    "admin_product_profile",
    "admin_product_image_description",
    "admin_product_faq_upsert",
    "admin_product_faq_delete",
    "admin_product_sku_create",
    "admin_product_sku_update",
    "admin_product_sku_disable",
    "admin_product_sku_image_replace",
    "admin_product_detail_section_upsert",
    "admin_product_detail_section_delete",
    "admin_product_review",
    "admin_order_cancel",
    "admin_shipment_progress",
    "admin_refund_decision",
    "admin_refund_more_info",
    "admin_support_claim",
    "admin_support_reply",
    "admin_support_resolve",
    "admin_dead_letter_replay_request",
    "admin_knowledge_document_publish",
    "admin_knowledge_document_withdraw",
    "admin_ai_agent_prompt_draft_create",
    "admin_ai_agent_publish_request",
    "admin_ai_skill_publish_request",
    "admin_ai_tool_publish_request",
    "admin_ai_evaluation_run",
]


class AgentRunView(StrictRequest):
    run_id: str
    conversation_id: str
    status: AgentRunStatus
    current_phase: str
    output: str | None
    error_code: str | None
    degraded_reason: str | None
    created_at: datetime
    updated_at: datetime


class AdminAgentRunView(StrictRequest):
    run_id: str
    status: AgentRunStatus
    current_phase: str
    agent_code: str
    agent_version_no: int
    conversation_type: Literal["exclusive", "store"]
    trace_id: str
    context_ref_count: int
    error_code: str | None
    degraded_reason: str | None
    available_actions: list[Literal["cancel"]]
    created_at: datetime
    updated_at: datetime
    version: int


class AdminAgentRunCancelRequest(StrictRequest):
    reason: str = Field(min_length=3, max_length=500)


class ModelProviderHealthView(StrictRequest):
    status: Literal["unconfigured", "available", "degraded", "unavailable"]
    provider: str
    configured_model: str | None
    model_available: bool
    available_models: list[str]
    chat_completions: bool
    structured_output: bool
    streaming: bool
    usage_reporting: bool
    checked_at: datetime
    latency_ms: int
    cache_hit: bool
    error_code: str | None


class AgentConsentGrantRequest(StrictRequest):
    consent_type: Literal["personalization", "order_read", "after_sale_write"]
    scope_type: Literal["user", "conversation", "store"]
    scope_id: str | None = Field(default=None, max_length=64)
    policy_version: str = Field(min_length=1, max_length=40)
    expires_at: datetime | None = None


class AgentConsentView(StrictRequest):
    consent_id: str
    consent_type: str
    scope_type: str
    scope_id: str | None
    policy_version: str
    status: AgentConsentStatus
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    version: int


class AgentConsentList(StrictRequest):
    items: list[AgentConsentView]


class AgentApprovalDecisionRequest(StrictRequest):
    decision: Literal["approve", "reject"]


class AgentApprovalView(StrictRequest):
    approval_id: str
    run_id: str
    conversation_id: str
    action_type: AgentApprovalActionType
    approval_status: Literal["pending", "approved", "rejected", "expired", "consumed"]
    decision: Literal["approve", "reject"] | None
    draft: dict[str, object]
    expires_at: datetime
    decided_at: datetime | None
    version: int
