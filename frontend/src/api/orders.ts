import { apiRequest, createIdempotencyKey, type ApiResult } from '@/api/http'
import type { CartData } from '@/api/cart'
import type { Money } from '@/api/catalog'

export const ORDER_VIEWS = ['all', 'pending_payment', 'pending_shipment', 'in_transit', 'completed', 'pending_review', 'after_sale', 'cancelled'] as const
export type OrderView = typeof ORDER_VIEWS[number]

export interface OrderAction {
  code: 'pay' | 'cancel_order' | 'apply_after_sale' | 'view_after_sale' | 'view_logistics' | 'review' | 'delete_order' | 'confirm_receipt' | 'contact_store' | 'repurchase'
  enabled: boolean
  reason_code: string | null
  reason_message: string | null
  requires_confirmation: boolean
  target: { type: 'route'; name: string; params: Record<string, string> }
}

export interface OrderItem {
  order_item_id: string
  product_id: string
  product_available: boolean
  sku_id: string
  product_name: string
  sku_name: string
  spec_snapshot: Array<{ name: string; value: string }>
  image_url: string | null
  quantity: number
  unit_price: Money
  gross_amount: Money
  payable_amount: Money
  refunded_amount: Money
  refunded_quantity: number
  review_status: string
  after_sale_status: string
}

export interface OrderSummary {
  order_id: string
  trade_order_id: string
  order_source: 'buy_now' | 'cart'
  store: { store_id: string; store_name: string; logo_url: string | null }
  order_status: string
  payment_status: string
  fulfillment_status: string
  after_sale_status: string
  matched_views: OrderView[]
  items: OrderItem[]
  item_count: number
  total_quantity: number
  amounts: {
    goods_amount: Money
    freight_amount: Money
    adjustment_amount: Money
    payable_amount: Money
    paid_amount: Money
    refunded_amount: Money
  }
  created_at: string
  expires_at: string
  available_actions: OrderAction[]
  version: number
}

export interface OrderEvent {
  event_id: number
  state_dimension: string
  from_status: string | null
  to_status: string
  event_code: string
  actor_type: string
  reason: string | null
  order_version: number
  occurred_at: string
}

export interface OrderDetail extends OrderSummary {
  buyer_remark: string | null
  address: {
    recipient_name: string
    phone_masked: string
    country_code: string
    province_code: string
    city_code: string
    district_code: string
    address: string
    postal_code: string | null
  }
  policy_snapshot: Record<string, unknown>
  events: OrderEvent[]
}

export interface OrderFilters {
  view: OrderView
  q?: string
  created_from?: string
  created_to?: string
  cursor?: string
  limit?: number
}

export interface OrderCommandResult { order: OrderSummary; events: OrderEvent[] }
export interface TradeOrder {
  trade_order_id: string
  trade_status: string
  amounts: OrderSummary['amounts']
  order_count: number
  expires_at: string
  available_actions: OrderAction[]
  version: number
}
export interface OrderHideResult { order_id: string; undo_until: string; restore_url: string; version: number }
export interface OrderRepurchaseResult {
  order_id: string
  added_items: string[]
  unavailable_items: Array<{ order_item_id: string; sku_id: string; product_name: string; reason_code: string; reason_message: string }>
  requires_reselection: boolean
  cart: CartData
}

export interface AdminOrderSummary {
  order: OrderSummary
  user_id: string
  user_name_masked: string
  available_admin_actions: Array<'adjust_amount' | 'cancel' | 'create_shipment'>
}
export interface AdminOrderDetail extends AdminOrderSummary { events: OrderEvent[] }

export function listMyOrders(filters: OrderFilters, token: string): Promise<ApiResult<{ items: OrderSummary[] }>> {
  const query = new URLSearchParams({ view: filters.view, limit: String(filters.limit ?? 10) })
  if (filters.q) query.set('q', filters.q)
  if (filters.created_from) query.set('created_from', filters.created_from)
  if (filters.created_to) query.set('created_to', filters.created_to)
  if (filters.cursor) query.set('cursor', filters.cursor)
  return apiRequest(`/users/me/orders?${query.toString()}`, {}, token)
}

export function getMyOrder(orderId: string, token: string): Promise<ApiResult<OrderDetail>> {
  return apiRequest(`/orders/${encodeURIComponent(orderId)}`, {}, token)
}

export function getMyTradeOrder(tradeOrderId: string, token: string): Promise<ApiResult<TradeOrder>> {
  return apiRequest(`/trade-orders/${encodeURIComponent(tradeOrderId)}`, {}, token)
}

export function cancelOrder(orderId: string, version: number, token: string, reasonCode = 'no_longer_needed', description?: string): Promise<ApiResult<OrderCommandResult>> {
  return apiRequest(`/orders/${encodeURIComponent(orderId)}/cancellations`, { method: 'POST', headers: { 'If-Match': `"v${version}"`, 'Idempotency-Key': createIdempotencyKey('order-cancel') }, body: JSON.stringify({ reason_code: reasonCode, ...(description ? { description } : {}) }) }, token)
}

export function confirmOrderReceipt(orderId: string, version: number, token: string): Promise<ApiResult<OrderCommandResult>> {
  return apiRequest(`/orders/${encodeURIComponent(orderId)}/receipt-confirmations`, { method: 'POST', headers: { 'If-Match': `"v${version}"`, 'Idempotency-Key': createIdempotencyKey('order-receipt') } }, token)
}

export function hideOrder(orderId: string, version: number, token: string): Promise<ApiResult<OrderHideResult>> {
  return apiRequest(`/users/me/orders/${encodeURIComponent(orderId)}`, { method: 'DELETE', headers: { 'If-Match': `"v${version}"` } }, token)
}

export function restoreOrder(result: OrderHideResult, token: string): Promise<ApiResult<OrderSummary>> {
  const path = result.restore_url.replace(/^\/api\/v1/, '')
  return apiRequest(path, { method: 'POST', headers: { 'If-Match': `"v${result.version}"`, 'Idempotency-Key': createIdempotencyKey('order-restore') } }, token)
}

export function repurchaseOrder(orderId: string, cartVersion: number, token: string): Promise<ApiResult<OrderRepurchaseResult>> {
  return apiRequest(`/orders/${encodeURIComponent(orderId)}/repurchases`, { method: 'POST', headers: { 'If-Match': `"v${cartVersion}"`, 'Idempotency-Key': createIdempotencyKey('order-repurchase') } }, token)
}

export function listAdminOrders(filters: Record<string, string>, token: string): Promise<ApiResult<{ items: AdminOrderSummary[] }>> {
  const query = new URLSearchParams(Object.entries(filters).filter(([, value]) => value))
  query.set('limit', '100')
  return apiRequest(`/admin/orders?${query.toString()}`, {}, token)
}

export function getAdminOrder(orderId: string, token: string): Promise<ApiResult<AdminOrderDetail>> {
  return apiRequest(`/admin/orders/${encodeURIComponent(orderId)}`, {}, token)
}

export function adjustAdminOrderAmount(orderId: string, etag: string, adjustmentMinorUnits: string, currency: string, reasonCode: string, reason: string, token: string): Promise<ApiResult<AdminOrderDetail>> {
  return apiRequest(`/admin/orders/${encodeURIComponent(orderId)}/amount-adjustments`, {
    method: 'POST',
    headers: { 'If-Match': etag, 'Idempotency-Key': createIdempotencyKey('admin-order-adjust') },
    body: JSON.stringify({ adjustment_amount: { minor_units: adjustmentMinorUnits, currency }, reason_code: reasonCode, reason }),
  }, token)
}

export function cancelAdminOrder(orderId: string, etag: string, reasonCode: string, reason: string, token: string): Promise<ApiResult<AdminOrderDetail>> {
  return apiRequest(`/admin/orders/${encodeURIComponent(orderId)}/cancellations`, {
    method: 'POST',
    headers: { 'If-Match': etag, 'Idempotency-Key': createIdempotencyKey('admin-order-cancel') },
    body: JSON.stringify({ reason_code: reasonCode, reason }),
  }, token)
}
