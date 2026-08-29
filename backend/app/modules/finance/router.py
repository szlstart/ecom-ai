from typing import Annotated, cast

from fastapi import APIRouter, Query, Response, status

from app.api.dependencies import IdempotencyKey, MerchantContext, UserContext
from app.api.schemas import Envelope
from app.modules.finance.dependencies import (
    AccountDeletionServiceDependency,
    FinanceServiceDependency,
)
from app.modules.finance.schemas import (
    AccountDeletionRequest,
    AccountDeletionTaskStatus,
    AccountDeletionTaskView,
    AdminStoreRevenueView,
    MerchantAccountDeletionRequest,
    MerchantRevenueView,
    WalletRechargeRequest,
    WalletRechargeResult,
    WalletTransactionList,
    WalletView,
)
from app.modules.identity.router import _no_store
from app.modules.rbac.dependencies import AdminAccess, require_admin_permission

router = APIRouter(prefix="/users/me/wallet", tags=["wallet"])
merchant_router = APIRouter(prefix="/merchant", tags=["merchant-finance"])
admin_router = APIRouter(prefix="/admin", tags=["admin-finance"])


account_router = APIRouter(prefix="/users/me", tags=["current-user"])


@account_router.delete(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Envelope[AccountDeletionTaskView],
    operation_id="UserAccount_DeleteMine",
)
async def delete_user_account(
    payload: AccountDeletionRequest,
    context: UserContext,
    service: AccountDeletionServiceDependency,
) -> Envelope[AccountDeletionTaskView]:
    del payload
    task = await service.delete_consumer(context.user)
    return Envelope(
        data=AccountDeletionTaskView(
            task_id=task.task_no,
            status=cast(AccountDeletionTaskStatus, task.task_status),
            phase=task.current_phase,
            requested_at=task.created_at,
        )
    )


@merchant_router.delete(
    "/account",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Envelope[AccountDeletionTaskView],
    operation_id="MerchantAccount_DeleteMine",
)
async def delete_merchant_account(
    payload: MerchantAccountDeletionRequest,
    context: MerchantContext,
    service: AccountDeletionServiceDependency,
) -> Envelope[AccountDeletionTaskView]:
    del payload
    task = await service.delete_merchant(context.user)
    return Envelope(
        data=AccountDeletionTaskView(
            task_id=task.task_no,
            status=cast(AccountDeletionTaskStatus, task.task_status),
            phase=task.current_phase,
            requested_at=task.created_at,
        )
    )


@router.get("", response_model=Envelope[WalletView], operation_id="UserWallet_GetMine")
async def get_wallet(
    response: Response, context: UserContext, service: FinanceServiceDependency
) -> Envelope[WalletView]:
    _no_store(response)
    return Envelope(data=await service.wallet(context.user))


@router.post(
    "/recharges",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[WalletRechargeResult],
    operation_id="UserWalletRecharge_Create",
)
async def create_recharge(
    payload: WalletRechargeRequest,
    response: Response,
    context: UserContext,
    service: FinanceServiceDependency,
    idempotency_key: IdempotencyKey,
) -> Envelope[WalletRechargeResult]:
    _no_store(response)
    return Envelope(data=await service.recharge(context.user, payload, idempotency_key))


@router.get(
    "/transactions",
    response_model=Envelope[WalletTransactionList],
    operation_id="UserWalletTransaction_ListMine",
)
async def list_transactions(
    response: Response,
    context: UserContext,
    service: FinanceServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Envelope[WalletTransactionList]:
    _no_store(response)
    return Envelope(data=await service.transactions(context.user, limit))


@merchant_router.get(
    "/stores/{store_id}/revenue",
    response_model=Envelope[MerchantRevenueView],
    operation_id="MerchantStoreRevenue_Get",
)
async def get_merchant_revenue(
    store_id: str,
    response: Response,
    context: MerchantContext,
    service: FinanceServiceDependency,
) -> Envelope[MerchantRevenueView]:
    _no_store(response)
    return Envelope(data=await service.merchant_revenue(context.user, store_id))


@admin_router.get(
    "/stores/{store_id}/revenue",
    response_model=Envelope[AdminStoreRevenueView],
    operation_id="AdminStoreRevenue_Get",
)
async def get_admin_store_revenue(
    store_id: str,
    response: Response,
    service: FinanceServiceDependency,
    access: Annotated[AdminAccess, require_admin_permission("stores:read")],
) -> Envelope[AdminStoreRevenueView]:
    _no_store(response)
    return Envelope(data=await service.admin_store_revenue(access, store_id))
