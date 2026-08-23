import { apiRequest, type ApiResult } from '@/api/http'
import type { Money } from '@/api/catalog'

export type OrderView = 'all' | 'pending_payment' | 'pending_shipment' | 'in_transit' | 'completed' | 'pending_review' | 'after_sale' | 'cancelled'

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
