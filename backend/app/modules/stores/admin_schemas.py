from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.api.schemas import StrictRequest
from app.modules.catalog.schemas import Money


class AdminStoreView(StrictRequest):
    store_id: str
    owner_user_id: str
    store_name: str
    description: str | None
    logo_file_id: str | None
    logo_url: str | None
    status: str
    suspension_source: Literal["merchant", "platform"] | None
    rating_score: str
    rating_count: int
    follower_count: int
    sales_count: int
    product_count: int | None = None
    net_revenue: Money | None = None
    store_name_changed_at: datetime | None
    store_name_change_available_at: datetime | None
    opened_at: datetime | None
    suspended_at: datetime | None
    closed_at: datetime | None
    version: int


class AdminStoreCreateRequest(StrictRequest):
    store_name: str = Field(min_length=2, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    merchant_username: str = Field(min_length=4, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    merchant_password: str = Field(min_length=1, max_length=128)
    merchant_email: str = Field(min_length=3, max_length=254)

    @field_validator("store_name")
    @classmethod
    def normalize_store_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("店铺名称至少需要 2 个字符")
        return normalized

    @field_validator("merchant_password")
    @classmethod
    def reject_password_whitespace(cls, value: str) -> str:
        if any(character.isspace() for character in value):
            raise ValueError("密码不能包含空白字符")
        return value


class AdminStoreDeleteRequest(StrictRequest):
    reason: str = Field(min_length=2, max_length=500)
    confirmation: str = Field(pattern=r"^DELETE_STORE$")


class AdminStoreUpdateRequest(StrictRequest):
    store_name: str | None = Field(default=None, min_length=2, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    logo_file_id: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def require_change(self) -> AdminStoreUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("at least one field must be supplied")
        return self


class AdminStoreList(StrictRequest):
    items: list[AdminStoreView]
    next_cursor: str | None


class AdminStoreStatusChangeRequest(StrictRequest):
    action: Literal["suspend", "resume"]
    confirmed: Literal[True]
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    reason: str = Field(min_length=2, max_length=500)


class AdminCertificationSummary(StrictRequest):
    certification_id: str
    store_id: str
    store_name: str
    certification_type: str
    review_status: str
    material_version: int
    valid_from: date | None
    valid_until: date | None
    reviewed_at: datetime | None
    version: int


class AdminCertificationList(StrictRequest):
    items: list[AdminCertificationSummary]
    next_cursor: str | None


class AdminCertificationDetail(AdminCertificationSummary):
    evidence_file_ids: list[str]
    decision_reason_code: str | None
    decision_reason: str | None


class AdminCertificationRequiredMaterial(StrictRequest):
    material_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    title: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=500)
    due_at: datetime | None = None


class AdminCertificationDecisionRequest(StrictRequest):
    decision: Literal["approve", "reject", "request_more_info"]
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    reason: str = Field(min_length=2, max_length=1000)
    valid_from: date | None = None
    valid_until: date | None = None
    required_materials: list[AdminCertificationRequiredMaterial] | None = Field(
        default=None,
        min_length=1,
        max_length=20,
    )

    @model_validator(mode="after")
    def validate_approval_dates(self) -> AdminCertificationDecisionRequest:
        if self.decision == "approve":
            if self.valid_from is None or self.valid_until is None:
                raise ValueError("approval requires valid_from and valid_until")
            if self.valid_until < self.valid_from:
                raise ValueError("valid_until must not precede valid_from")
        elif self.valid_from is not None or self.valid_until is not None:
            raise ValueError("validity dates are only accepted for approval")
        if self.decision == "request_more_info" and not self.required_materials:
            raise ValueError("request_more_info requires required_materials")
        if self.decision != "request_more_info" and self.required_materials is not None:
            raise ValueError("required_materials is only accepted for request_more_info")
        return self


class AdminCertificationMaterialRequest(StrictRequest):
    evidence_file_ids: list[str] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=2, max_length=500)


class AdminCertificationEventView(StrictRequest):
    event_id: str
    event_type: str
    material_version: int
    evidence_file_ids: list[str]
    reason_code: str | None
    reason: str | None
    required_materials: list[dict[str, object]] | None
    actor_type: str
    certification_version: int
    created_at: datetime


class AdminStorePolicyView(StrictRequest):
    policy_id: str
    store_id: str
    policy_type: str
    title: str
    content: str
    policy_version: int
    status: str
    effective_at: datetime | None
    expires_at: datetime | None
    published_at: datetime | None
    withdrawn_at: datetime | None
    version: int


class AdminStorePolicyCreateRequest(StrictRequest):
    policy_type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")
    title: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=100_000)
    effective_at: datetime | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_window(self) -> AdminStorePolicyCreateRequest:
        if (
            self.effective_at is not None
            and self.expires_at is not None
            and self.expires_at <= self.effective_at
        ):
            raise ValueError("expires_at must be later than effective_at")
        return self


class AdminStorePolicyUpdateRequest(StrictRequest):
    title: str | None = Field(default=None, min_length=1, max_length=128)
    content: str | None = Field(default=None, min_length=1, max_length=100_000)
    effective_at: datetime | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_window(self) -> AdminStorePolicyUpdateRequest:
        if (
            self.effective_at is not None
            and self.expires_at is not None
            and self.expires_at <= self.effective_at
        ):
            raise ValueError("expires_at must be later than effective_at")
        return self


class AdminPolicyCommandRequest(StrictRequest):
    reason: str = Field(min_length=2, max_length=500)
