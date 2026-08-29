from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from app.api.dependencies import IdempotencyKey
from app.api.schemas import Envelope
from app.modules.cart.dependencies import CartServiceDependency
from app.modules.cart.schemas import CartItemPatchRequest, CartView
from app.modules.catalog.dependencies import CatalogServiceDependency
from app.modules.catalog.schemas import ProductList
from app.modules.finance.dependencies import AccountDeletionServiceDependency
from app.modules.identity.dependencies import IdentityServiceDependency
from app.modules.identity.router import _etag, _expected_version, _no_store
from app.modules.identity.schemas import (
    AddressList,
    AddressPatch,
    AddressView,
    AddressWrite,
    MessageResult,
)
from app.modules.orders.dependencies import OrderServiceDependency
from app.modules.orders.schemas import OrderCancellationRequest, OrderCommandResult
from app.modules.rbac.dependencies import (
    AdminAccess,
    require_admin_permission,
    require_any_admin_permission,
)
from app.modules.rbac.schemas import (
    AdminDashboardSummary,
    AdminUserCreateRequest,
    AdminUserList,
    AdminUserPasswordReplaceRequest,
    AdminUserSummary,
    AdminUserUpdateRequest,
    AdminUserWorkspace,
    AdminWalletAdjustmentRequest,
    AdminWalletAdjustmentResult,
    ApprovalDecisionRequest,
    ApprovalView,
    AuditLogView,
    PasswordResetRequirementRequest,
    ReasonRequest,
    RoleCreateRequest,
    RoleGrantCreateRequest,
    RoleGrantEventView,
    RoleGrantView,
    RolePermissionsReplaceRequest,
    RoleSummary,
    RoleUpdateRequest,
    SensitiveFields,
    SensitiveGrantCreateRequest,
    SensitiveGrantResult,
    SessionRevocationRequest,
    UserStatusChangeRequest,
    UserStatusEventView,
)
from app.modules.rbac.service_dependencies import RbacServiceDependency
from app.modules.stores.dependencies import StoreServiceDependency
from app.modules.stores.schemas import FollowedStoreList

router = APIRouter(prefix="/admin", tags=["administration"])


@router.get(
    "/dashboard",
    response_model=Envelope[AdminDashboardSummary],
    operation_id="AdminDashboard_Get",
)
async def get_dashboard(
    response: Response,
    service: RbacServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("dashboard:read")],
) -> Envelope[AdminDashboardSummary]:
    _no_store(response)
    return Envelope(data=await service.dashboard(access))


@router.get(
    "/users",
    response_model=Envelope[AdminUserList],
    operation_id="AdminUser_List",
)
async def list_users(
    response: Response,
    service: RbacServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:read")],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=40)] = None,
) -> Envelope[AdminUserList]:
    _no_store(response)
    return Envelope(data=await service.list_users(limit, cursor))


@router.post(
    "/users",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[AdminUserSummary],
    operation_id="AdminUser_Create",
)
async def create_user(
    payload: AdminUserCreateRequest,
    response: Response,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("users:manage")],
) -> Envelope[AdminUserSummary]:
    item = await service.create_user(access, payload, idempotency_key)
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.get(
    "/users/{user_id}",
    response_model=Envelope[AdminUserSummary],
    operation_id="AdminUser_Get",
)
async def get_user(
    user_id: str,
    response: Response,
    service: RbacServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:read")],
) -> Envelope[AdminUserSummary]:
    _no_store(response)
    item = await service.get_user(user_id)
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.get(
    "/users/{user_id}/workspace",
    response_model=Envelope[AdminUserWorkspace],
    operation_id="AdminUserWorkspace_Get",
)
async def get_user_workspace(
    user_id: str,
    response: Response,
    service: RbacServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:read")],
) -> Envelope[AdminUserWorkspace]:
    _no_store(response)
    return Envelope(data=await service.get_user_workspace(user_id))


@router.get(
    "/users/{user_id}/addresses",
    response_model=Envelope[AddressList],
    operation_id="AdminUserAddress_List",
)
async def list_admin_user_addresses(
    user_id: str,
    response: Response,
    service: RbacServiceDependency,
    identity_service: IdentityServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:read")],
) -> Envelope[AddressList]:
    target = await service.require_consumer_user(user_id)
    _no_store(response)
    return Envelope(data=await identity_service.list_addresses(target.id))


@router.post(
    "/users/{user_id}/addresses",
    response_model=Envelope[AddressView],
    status_code=status.HTTP_201_CREATED,
    operation_id="AdminUserAddress_Create",
)
async def create_admin_user_address(
    user_id: str,
    payload: AddressWrite,
    service: RbacServiceDependency,
    identity_service: IdentityServiceDependency,
    idempotency_key: IdempotencyKey,
    _access: Annotated[AdminAccess, require_admin_permission("users:manage")],
) -> Envelope[AddressView]:
    target = await service.require_consumer_user(user_id)
    return Envelope(data=await identity_service.create_address(target, payload, idempotency_key))


@router.patch(
    "/users/{user_id}/addresses/{address_id}",
    response_model=Envelope[AddressView],
    operation_id="AdminUserAddress_Update",
)
async def update_admin_user_address(
    user_id: str,
    address_id: str,
    payload: AddressPatch,
    service: RbacServiceDependency,
    identity_service: IdentityServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AddressView]:
    target = await service.require_consumer_user(user_id)
    return Envelope(
        data=await identity_service.update_address(
            target.id, address_id, payload, _expected_version(if_match)
        )
    )


@router.delete(
    "/users/{user_id}/addresses/{address_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="AdminUserAddress_Delete",
)
async def delete_admin_user_address(
    user_id: str,
    address_id: str,
    service: RbacServiceDependency,
    identity_service: IdentityServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> None:
    target = await service.require_consumer_user(user_id)
    await identity_service.delete_address(target.id, address_id, _expected_version(if_match))


@router.get(
    "/users/{user_id}/cart",
    response_model=Envelope[CartView],
    operation_id="AdminUserCart_Get",
)
async def get_admin_user_cart(
    user_id: str,
    service: RbacServiceDependency,
    cart_service: CartServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:read")],
) -> Envelope[CartView]:
    target = await service.require_consumer_user(user_id)
    return Envelope(data=await cart_service.get(target))


@router.patch(
    "/users/{user_id}/cart/items/{item_id}",
    response_model=Envelope[CartView],
    operation_id="AdminUserCartItem_Update",
)
async def update_admin_user_cart_item(
    user_id: str,
    item_id: str,
    payload: CartItemPatchRequest,
    service: RbacServiceDependency,
    cart_service: CartServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[CartView]:
    target = await service.require_consumer_user(user_id)
    return Envelope(
        data=await cart_service.patch(target, item_id, payload, _expected_version(if_match))
    )


@router.delete(
    "/users/{user_id}/cart/items/{item_id}",
    response_model=Envelope[CartView],
    operation_id="AdminUserCartItem_Delete",
)
async def delete_admin_user_cart_item(
    user_id: str,
    item_id: str,
    service: RbacServiceDependency,
    cart_service: CartServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[CartView]:
    target = await service.require_consumer_user(user_id)
    return Envelope(data=await cart_service.delete(target, item_id, _expected_version(if_match)))


@router.get(
    "/users/{user_id}/favorite-products",
    response_model=Envelope[ProductList],
    operation_id="AdminUserFavoriteProduct_List",
)
async def list_admin_user_favorite_products(
    user_id: str,
    service: RbacServiceDependency,
    catalog_service: CatalogServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:read")],
) -> Envelope[ProductList]:
    target = await service.require_consumer_user(user_id)
    return Envelope(data=await catalog_service.favorite_products(target.id, 50))


@router.delete(
    "/users/{user_id}/favorite-products/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="AdminUserFavoriteProduct_Delete",
)
async def delete_admin_user_favorite_product(
    user_id: str,
    product_id: str,
    service: RbacServiceDependency,
    catalog_service: CatalogServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:manage")],
) -> None:
    target = await service.require_consumer_user(user_id)
    await catalog_service.set_favorite(target.id, product_id, False)


@router.get(
    "/users/{user_id}/followed-stores",
    response_model=Envelope[FollowedStoreList],
    operation_id="AdminUserFollowedStore_List",
)
async def list_admin_user_followed_stores(
    user_id: str,
    service: RbacServiceDependency,
    store_service: StoreServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:read")],
) -> Envelope[FollowedStoreList]:
    target = await service.require_consumer_user(user_id)
    return Envelope(data=await store_service.followed_stores(target.id, 50))


@router.delete(
    "/users/{user_id}/followed-stores/{store_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="AdminUserFollowedStore_Delete",
)
async def delete_admin_user_followed_store(
    user_id: str,
    store_id: str,
    service: RbacServiceDependency,
    store_service: StoreServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:manage")],
) -> None:
    target = await service.require_consumer_user(user_id)
    await store_service.set_follow(target.id, store_id, False)


@router.post(
    "/users/{user_id}/orders/{order_id}/cancellations",
    response_model=Envelope[OrderCommandResult],
    operation_id="AdminUserOrder_CancelAsUser",
)
async def cancel_admin_user_order(
    user_id: str,
    order_id: str,
    service: RbacServiceDependency,
    order_service: OrderServiceDependency,
    idempotency_key: IdempotencyKey,
    _access: Annotated[AdminAccess, require_admin_permission("users:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[OrderCommandResult]:
    target = await service.require_consumer_user(user_id)
    return Envelope(
        data=await order_service.cancel(
            target,
            order_id,
            OrderCancellationRequest(reason_code="other", description="超级管理员代用户取消"),
            _expected_version(if_match),
            idempotency_key,
        )
    )


@router.post(
    "/users/{user_id}/orders/{order_id}/receipt-confirmations",
    response_model=Envelope[OrderCommandResult],
    operation_id="AdminUserOrder_ConfirmReceiptAsUser",
)
async def confirm_admin_user_order_receipt(
    user_id: str,
    order_id: str,
    service: RbacServiceDependency,
    order_service: OrderServiceDependency,
    idempotency_key: IdempotencyKey,
    _access: Annotated[AdminAccess, require_admin_permission("users:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[OrderCommandResult]:
    target = await service.require_consumer_user(user_id)
    return Envelope(
        data=await order_service.confirm_receipt(
            target, order_id, _expected_version(if_match), idempotency_key
        )
    )


@router.patch(
    "/users/{user_id}",
    response_model=Envelope[AdminUserSummary],
    operation_id="AdminUser_Update",
)
async def update_user(
    user_id: str,
    payload: AdminUserUpdateRequest,
    response: Response,
    service: RbacServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("users:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminUserSummary]:
    item = await service.update_user(access, user_id, payload, _expected_version(if_match))
    response.headers["ETag"] = _etag(item.version)
    _no_store(response)
    return Envelope(data=item)


@router.post(
    "/users/{user_id}/password-replacements",
    response_model=Envelope[MessageResult],
    operation_id="AdminUserPassword_Replace",
)
async def replace_user_password(
    user_id: str,
    payload: AdminUserPasswordReplaceRequest,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("users:force_password_reset")],
) -> Envelope[MessageResult]:
    await service.replace_user_password(access, user_id, payload, idempotency_key)
    return Envelope(data=MessageResult(message="临时密码已设置，目标账号的全部会话已撤销。"))


@router.post(
    "/users/{user_id}/wallet-adjustments",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[AdminWalletAdjustmentResult],
    operation_id="AdminUserWallet_Adjust",
)
async def adjust_user_wallet(
    user_id: str,
    payload: AdminWalletAdjustmentRequest,
    response: Response,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("users:manage")],
) -> Envelope[AdminWalletAdjustmentResult]:
    _no_store(response)
    return Envelope(
        data=await service.adjust_user_wallet(access, user_id, payload, idempotency_key)
    )


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="AdminUser_Delete",
)
async def delete_user(
    user_id: str,
    service: RbacServiceDependency,
    deletion_service: AccountDeletionServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("users:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> None:
    target = await service.prepare_user_deletion(
        access,
        user_id,
        _expected_version(if_match),
    )
    await deletion_service.delete_consumer(target)


@router.get(
    "/users/{user_id}/status-events",
    response_model=Envelope[list[UserStatusEventView]],
    operation_id="AdminUserStatusEvent_List",
)
async def list_user_status_events(
    user_id: str,
    response: Response,
    service: RbacServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("users:read")],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[list[UserStatusEventView]]:
    _no_store(response)
    return Envelope(data=await service.list_user_status_events(user_id, limit))


@router.post(
    "/users/{user_id}/status-changes",
    response_model=Envelope[AdminUserSummary],
    operation_id="AdminUser_ChangeStatus",
)
async def change_user_status(
    user_id: str,
    payload: UserStatusChangeRequest,
    response: Response,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("users:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[AdminUserSummary]:
    item = await service.change_user_status(
        access,
        user_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.post(
    "/users/{user_id}/session-revocations",
    response_model=Envelope[MessageResult],
    operation_id="AdminUserSession_Revoke",
)
async def revoke_user_sessions(
    user_id: str,
    payload: SessionRevocationRequest,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[
        AdminAccess,
        require_admin_permission("users:sessions_revoke"),
    ],
) -> Envelope[MessageResult]:
    await service.revoke_user_sessions(
        access,
        user_id,
        payload.reason,
        idempotency_key,
    )
    return Envelope(data=MessageResult(message="目标账号的登录会话已撤销。"))


@router.post(
    "/users/{user_id}/password-reset-requirements",
    response_model=Envelope[MessageResult],
    operation_id="AdminUserPasswordReset_Require",
)
async def require_password_reset(
    user_id: str,
    payload: PasswordResetRequirementRequest,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[
        AdminAccess,
        require_admin_permission("users:force_password_reset"),
    ],
) -> Envelope[MessageResult]:
    await service.require_password_reset(
        access,
        user_id,
        payload.reason,
        idempotency_key,
    )
    return Envelope(data=MessageResult(message="目标账号下次登录前必须完成密码重置。"))


@router.get(
    "/roles",
    response_model=Envelope[list[RoleSummary]],
    operation_id="AdminRole_List",
)
async def list_roles(
    service: RbacServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("rbac:read")],
) -> Envelope[list[RoleSummary]]:
    return Envelope(data=await service.list_roles())


@router.post(
    "/roles",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[RoleSummary],
    operation_id="AdminRole_Create",
)
async def create_role(
    payload: RoleCreateRequest,
    response: Response,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("rbac:manage")],
) -> Envelope[RoleSummary]:
    item = await service.create_role(access, payload, idempotency_key)
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.get(
    "/roles/{role_id}",
    response_model=Envelope[RoleSummary],
    operation_id="AdminRole_Get",
)
async def get_role(
    role_id: str,
    response: Response,
    service: RbacServiceDependency,
    _access: Annotated[AdminAccess, require_admin_permission("rbac:read")],
) -> Envelope[RoleSummary]:
    item = await service.get_role(role_id)
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.patch(
    "/roles/{role_id}",
    response_model=Envelope[RoleSummary],
    operation_id="AdminRole_Update",
)
async def update_role(
    role_id: str,
    payload: RoleUpdateRequest,
    response: Response,
    service: RbacServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("rbac:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[RoleSummary]:
    item = await service.update_role(access, role_id, payload, _expected_version(if_match))
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.put(
    "/roles/{role_id}/permissions",
    response_model=Envelope[RoleSummary],
    operation_id="AdminRolePermission_Replace",
)
async def replace_role_permissions(
    role_id: str,
    payload: RolePermissionsReplaceRequest,
    response: Response,
    service: RbacServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("rbac:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[RoleSummary]:
    item = await service.replace_role_permissions(
        access,
        role_id,
        payload,
        _expected_version(if_match),
    )
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.get(
    "/users/{user_id}/role-grants",
    response_model=Envelope[list[RoleGrantView]],
    operation_id="AdminRoleGrant_List",
)
async def list_role_grants(
    user_id: str,
    service: RbacServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("rbac:read")],
) -> Envelope[list[RoleGrantView]]:
    return Envelope(data=await service.list_role_grants(access, user_id))


@router.get(
    "/users/{user_id}/role-grant-events",
    response_model=Envelope[list[RoleGrantEventView]],
    operation_id="AdminRoleGrantEvent_List",
)
async def list_role_grant_events(
    user_id: str,
    service: RbacServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("rbac:read")],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[list[RoleGrantEventView]]:
    return Envelope(data=await service.list_role_grant_events(access, user_id, limit))


@router.post(
    "/users/{user_id}/role-grants",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[RoleGrantView],
    operation_id="AdminRoleGrant_Create",
)
async def create_role_grant(
    user_id: str,
    payload: RoleGrantCreateRequest,
    response: Response,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("rbac:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[RoleGrantView]:
    item = await service.create_role_grant(
        access,
        user_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.post(
    "/users/{user_id}/role-grants/{grant_id}/revocations",
    response_model=Envelope[MessageResult],
    operation_id="AdminRoleGrant_Revoke",
)
async def revoke_role_grant(
    user_id: str,
    grant_id: str,
    payload: ReasonRequest,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[AdminAccess, require_admin_permission("rbac:manage")],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[MessageResult]:
    await service.revoke_role_grant(
        access,
        user_id,
        grant_id,
        payload.reason,
        _expected_version(if_match),
        idempotency_key,
    )
    return Envelope(data=MessageResult(message="角色授权已撤销。"))


@router.post(
    "/users/{user_id}/sensitive-field-access-grants",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[SensitiveGrantResult],
    operation_id="AdminSensitiveGrant_Create",
)
async def create_sensitive_grant(
    user_id: str,
    payload: SensitiveGrantCreateRequest,
    response: Response,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[
        AdminAccess,
        require_admin_permission("users:read_sensitive"),
    ],
) -> Envelope[SensitiveGrantResult]:
    _no_store(response)
    item = await service.create_sensitive_grant(
        access,
        user_id,
        payload,
        idempotency_key,
    )
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.post(
    "/sensitive-field-access-grants/{grant_id}/revocations",
    response_model=Envelope[MessageResult],
    operation_id="AdminSensitiveGrant_Revoke",
)
async def revoke_sensitive_grant(
    grant_id: str,
    payload: ReasonRequest,
    response: Response,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[
        AdminAccess,
        require_any_admin_permission("users:read_sensitive", "users:manage"),
    ],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[MessageResult]:
    version = await service.revoke_sensitive_grant(
        access,
        grant_id,
        payload.reason,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(version)
    return Envelope(data=MessageResult(message="敏感字段访问凭据已撤销。"))


@router.get(
    "/users/{user_id}/sensitive-fields",
    response_model=Envelope[SensitiveFields],
    operation_id="AdminSensitiveFields_Get",
)
async def get_sensitive_fields(
    user_id: str,
    response: Response,
    service: RbacServiceDependency,
    access: Annotated[
        AdminAccess,
        require_admin_permission("users:read_sensitive"),
    ],
    grant_id: Annotated[str, Header(alias="X-Sensitive-Access-Grant")],
) -> Envelope[SensitiveFields]:
    _no_store(response)
    return Envelope(data=await service.consume_sensitive_grant(access, user_id, grant_id))


@router.get(
    "/approval-requests",
    response_model=Envelope[list[ApprovalView]],
    operation_id="AdminApproval_List",
)
async def list_approvals(
    service: RbacServiceDependency,
    access: Annotated[
        AdminAccess,
        require_admin_permission("admin_approvals:read"),
    ],
) -> Envelope[list[ApprovalView]]:
    return Envelope(data=await service.list_approvals(access))


@router.get(
    "/approval-requests/{approval_request_id}",
    response_model=Envelope[ApprovalView],
    operation_id="AdminApproval_Get",
)
async def get_approval(
    approval_request_id: str,
    response: Response,
    service: RbacServiceDependency,
    access: Annotated[
        AdminAccess,
        require_admin_permission("admin_approvals:read"),
    ],
) -> Envelope[ApprovalView]:
    item = await service.get_approval(access, approval_request_id)
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.post(
    "/approval-requests/{approval_request_id}/decisions",
    response_model=Envelope[ApprovalView],
    operation_id="AdminApproval_Decide",
)
async def decide_approval(
    approval_request_id: str,
    payload: ApprovalDecisionRequest,
    response: Response,
    service: RbacServiceDependency,
    idempotency_key: IdempotencyKey,
    access: Annotated[
        AdminAccess,
        require_admin_permission("admin_approvals:decide"),
    ],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Envelope[ApprovalView]:
    item = await service.decide_approval(
        access,
        approval_request_id,
        payload,
        _expected_version(if_match),
        idempotency_key,
    )
    response.headers["ETag"] = _etag(item.version)
    return Envelope(data=item)


@router.get(
    "/audit-logs",
    response_model=Envelope[list[AuditLogView]],
    operation_id="AdminAudit_List",
)
async def list_audit_logs(
    response: Response,
    service: RbacServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("audit:read")],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[list[AuditLogView]]:
    _no_store(response)
    return Envelope(data=await service.list_audit_logs(access, limit))
