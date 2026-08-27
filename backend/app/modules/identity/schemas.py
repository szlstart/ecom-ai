from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.api.schemas import StrictRequest


class AgreementReference(StrictRequest):
    document_type: Literal["terms_of_service", "privacy_policy"]
    document_version: str


class RegistrationRequest(StrictRequest):
    username: str = Field(min_length=4, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    email: str = Field(min_length=3, max_length=254)
    captcha_id: str = Field(min_length=16, max_length=128)
    captcha_answer: str = Field(pattern=r"^[0-9]{1,3}$")
    password: str = Field(min_length=1)
    config_version: str
    agreement_acceptances: list[AgreementReference] = Field(min_length=2, max_length=2)
    locale: str = Field(default="zh-CN", max_length=16)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)


class VerificationCodeRequest(StrictRequest):
    purpose: Literal["reset_password", "change_phone", "change_email"]
    target_type: Literal["phone", "email"]
    target: str = Field(min_length=3, max_length=254)
    locale: str = Field(default="zh-CN", max_length=16)
    challenge_token: str | None = Field(default=None, max_length=2048)
    change_ticket_id: str | None = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def purpose_matches_target(self) -> VerificationCodeRequest:
        if self.purpose == "change_phone" and self.target_type != "phone":
            raise ValueError("change_phone requires target_type=phone")
        if self.purpose == "change_email" and self.target_type != "email":
            raise ValueError("change_email requires target_type=email")
        return self


class ClientDescriptor(StrictRequest):
    client_type: Literal["web"] = "web"
    device_name: str = Field(min_length=1, max_length=128)


class PasswordLoginRequest(StrictRequest):
    auth_method: Literal["password"]
    identifier: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1)
    client: ClientDescriptor
    challenge_token: str | None = Field(default=None, max_length=2048)


LoginRequest = PasswordLoginRequest


class UserSummary(StrictRequest):
    user_id: str
    username: str
    nickname: str
    avatar_url: str | None
    account_status: str


class SessionSummary(StrictRequest):
    session_id: str
    client_type: str
    device_name: str | None
    audience: str
    authenticated_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    is_current: bool = False


class SessionBootstrap(StrictRequest):
    user: UserSummary
    session: SessionSummary
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    csrf_token: str


class VerificationCodeAccepted(StrictRequest):
    verification_id: str
    delivery_status: Literal["accepted"] = "accepted"
    target_masked: str
    expires_at: datetime
    retry_after_seconds: int


class PasswordResetHintRequest(StrictRequest):
    username: str = Field(min_length=4, max_length=32, pattern=r"^[A-Za-z0-9_]+$")


class PasswordResetHintResult(StrictRequest):
    email_masked: str


class PasswordResetTicketRequest(StrictRequest):
    username: str = Field(min_length=4, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    email: str = Field(min_length=3, max_length=254)


class PasswordResetTicketResult(StrictRequest):
    reset_ticket: str
    expires_at: datetime


class PasswordResetRequest(StrictRequest):
    reset_ticket: str = Field(min_length=32, max_length=256)
    new_password: str = Field(min_length=1)


class MessageResult(StrictRequest):
    message: str


class UserProfile(StrictRequest):
    user_id: str
    username: str
    nickname: str
    avatar_url: str | None
    account_status: str
    locale: str
    timezone: str
    bound_accounts: list[dict[str, str | bool]]
    version: int


class UserProfileUpdate(StrictRequest):
    nickname: str | None = Field(default=None, min_length=2, max_length=20)
    avatar_file_id: str | None = Field(default=None, max_length=40)
    locale: str | None = Field(default=None, max_length=16)
    timezone: str | None = Field(default=None, max_length=64)


class PasswordChangeRequest(StrictRequest):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


class SecuritySummary(StrictRequest):
    password_set: bool
    password_changed_at: datetime | None
    current_email: str | None
    bound_accounts: list[dict[str, str | bool]]
    active_session_count: int


class AddressWrite(StrictRequest):
    recipient_name: str = Field(min_length=1, max_length=64)
    phone: str = Field(min_length=7, max_length=32)
    country_code: str = Field(default="CN", pattern=r"^[A-Z]{2}$")
    province_code: str = Field(min_length=1, max_length=32)
    city_code: str = Field(min_length=1, max_length=32)
    district_code: str = Field(min_length=1, max_length=32)
    address: str = Field(min_length=2, max_length=500)
    postal_code: str | None = Field(default=None, max_length=16)
    label: str | None = Field(default=None, max_length=32)
    is_default: bool = False


class AddressPatch(StrictRequest):
    recipient_name: str | None = Field(default=None, min_length=1, max_length=64)
    phone: str | None = Field(default=None, min_length=7, max_length=32)
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    province_code: str | None = Field(default=None, min_length=1, max_length=32)
    city_code: str | None = Field(default=None, min_length=1, max_length=32)
    district_code: str | None = Field(default=None, min_length=1, max_length=32)
    address: str | None = Field(default=None, min_length=2, max_length=500)
    postal_code: str | None = Field(default=None, max_length=16)
    label: str | None = Field(default=None, max_length=32)


class AddressView(StrictRequest):
    address_id: str
    recipient_name: str
    phone: str
    phone_masked: str
    country_code: str
    province_code: str
    city_code: str
    district_code: str
    address: str
    postal_code: str | None
    label: str | None
    is_default: bool
    version: int


class AddressList(StrictRequest):
    items: list[AddressView]
    active_count: int
    max_count: int = 20
    can_create: bool


class DefaultAddressRequest(StrictRequest):
    address_id: str


class ContactChangeTicketRequest(StrictRequest):
    credential_type: Literal["phone", "email"]
    current_password: str = Field(min_length=1)


class ContactChangeTicketResult(StrictRequest):
    change_ticket_id: str
    credential_type: Literal["phone", "email"]
    expires_at: datetime


class ContactChangeRequest(StrictRequest):
    new_email: str = Field(min_length=3, max_length=254)


class UserDashboard(StrictRequest):
    order_counts: dict[str, int]
    review_counts: dict[str, int]
    default_address: AddressView | None
    unread_message_count: int
    favorite_product_count: int
    followed_store_count: int
    unavailable_sections: list[str]
