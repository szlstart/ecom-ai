import { apiRequest, createIdempotencyKey, type ApiResult } from '@/api/http'
import type { Money, ServiceEstimate } from '@/api/catalog'

export interface CheckoutIssue { code: string; message: string; store_id: string | null; sku_id: string | null }
export interface CheckoutItem { product_id: string; sku_id: string; product_name: string; sku_name: string; spec_values: Array<Record<string, string>>; quantity: number; unit_price: Money; subtotal: Money; available_quantity: number }
export interface CheckoutStoreGroup { store_id: string; store_name: string; items: CheckoutItem[]; goods_amount: Money; freight_amount: Money; delivery_options: Array<{ option_id: string; name: string; freight: Money; estimate: ServiceEstimate }>; selected_delivery_option: string | null; buyer_remark: string | null; policy_versions: Record<string, number>; customer_service_context: Record<string, string> }
export interface CheckoutData { checkout_id: string; source_type: 'buy_now' | 'cart'; status: 'active' | 'submitted' | 'expired' | 'cancelled'; address_id: string | null; expires_at: string; store_groups: CheckoutStoreGroup[]; amounts: { goods_amount: Money; freight_amount: Money; payable_amount: Money }; warnings: CheckoutIssue[]; blocking_issues: CheckoutIssue[]; available_actions: string[]; pricing_version: string; version: number }
export interface AddressSummary { address_id: string; recipient_name: string; phone_masked: string; province_code: string; city_code: string; district_code: string; address: string; is_default: boolean }

export function createBuyNowCheckout(skuId: string, quantity: number, token: string, key = createIdempotencyKey('checkout')): Promise<ApiResult<CheckoutData>> {
  return apiRequest('/checkout-sessions', { method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify({ source: { source_type: 'buy_now', sku_id: skuId, quantity } }) }, token)
}
export function createCartCheckout(itemIds: string[], token: string, key = createIdempotencyKey('checkout')): Promise<ApiResult<CheckoutData>> {
  return apiRequest('/checkout-sessions', { method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify({ source: { source_type: 'cart', cart_item_ids: itemIds } }) }, token)
}
export function getCheckout(id: string, token: string): Promise<ApiResult<CheckoutData>> { return apiRequest(`/checkout-sessions/${encodeURIComponent(id)}`, {}, token) }
export function patchCheckout(id: string, payload: { address_id?: string; buyer_remarks?: Array<{ store_id: string; content: string }> }, version: number, token: string): Promise<ApiResult<CheckoutData>> {
  return apiRequest(`/checkout-sessions/${encodeURIComponent(id)}`, { method: 'PATCH', headers: { 'If-Match': `"v${version}"` }, body: JSON.stringify(payload) }, token)
}
export function repriceCheckout(id: string, token: string): Promise<ApiResult<CheckoutData>> {
  return apiRequest(`/checkout-sessions/${encodeURIComponent(id)}/repricings`, { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('reprice') } }, token)
}
export function listAddresses(token: string): Promise<ApiResult<{ items: AddressSummary[] }>> { return apiRequest('/users/me/addresses', {}, token) }

export interface OrderCreateResult { trade_order_id: string; order_ids: string[]; payment_deadline_at: string; available_actions: string[]; version: number }
export function createOrder(checkoutId: string, checkoutVersion: number, token: string, key: string): Promise<ApiResult<OrderCreateResult>> {
  return apiRequest('/orders', { method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify({ checkout_id: checkoutId, checkout_version: checkoutVersion }) }, token)
}
