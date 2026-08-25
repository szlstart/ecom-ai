import { apiRequest, createIdempotencyKey, type ApiResult } from '@/api/http'
import type { Money } from '@/api/catalog'

export type PaymentStatus = 'created' | 'pending' | 'succeeded' | 'failed' | 'closed' | 'partially_refunded' | 'refunded'

export interface Payment {
  payment_id: string
  trade_order_id: string
  provider: string
  payment_method: string
  payment_status: PaymentStatus
  display_status: 'confirming' | 'succeeded' | 'failed' | 'closed' | 'refunded'
  requested_amount: Money
  paid_amount: Money
  refunded_amount: Money
  expires_at: string
  paid_at: string | null
  closed_at: string | null
  failure_code: string | null
  failure_message: string | null
  action: { type: 'redirect'; url: string } | null
  events: Array<{ event_id: string; event_type: string; from_status: string | null; to_status: string; amount: Money; source_type: string; occurred_at: string }>
  version: number
}

export interface AdminPayment {
  payment: Payment
  user_id: string
  store_ids: string[]
  provider_trade_no_masked: string | null
  available_admin_actions: Array<'reconcile'>
}

export function listTradePayments(tradeOrderId: string, token: string): Promise<ApiResult<{ items: Payment[] }>> {
  return apiRequest(`/trade-orders/${encodeURIComponent(tradeOrderId)}/payments`, {}, token)
}

export function createPayment(tradeOrderId: string, token: string): Promise<ApiResult<Payment>> {
  return apiRequest('/payments', {
    method: 'POST',
    headers: { 'Idempotency-Key': createIdempotencyKey('payment-create') },
    body: JSON.stringify({ trade_order_id: tradeOrderId, provider: 'fake', payment_method: 'fake_balance', return_url_key: 'payment_result' }),
  }, token)
}

export function getPayment(paymentId: string, token: string): Promise<ApiResult<Payment>> {
  return apiRequest(`/payments/${encodeURIComponent(paymentId)}`, {}, token)
}

export function closePayment(payment: Payment, token: string): Promise<ApiResult<Payment>> {
  return apiRequest(`/payments/${encodeURIComponent(payment.payment_id)}/closures`, {
    method: 'POST',
    headers: { 'If-Match': `"v${payment.version}"`, 'Idempotency-Key': createIdempotencyKey('payment-close') },
  }, token)
}

export function listAdminPayments(filters: Record<string, string>, token: string): Promise<ApiResult<{ items: AdminPayment[] }>> {
  const query = new URLSearchParams(Object.entries(filters).filter(([, value]) => value))
  query.set('limit', '100')
  return apiRequest(`/admin/payments?${query.toString()}`, {}, token)
}

export function getAdminPayment(paymentId: string, token: string): Promise<ApiResult<AdminPayment>> {
  return apiRequest(`/admin/payments/${encodeURIComponent(paymentId)}`, {}, token)
}

export function reconcileAdminPayment(paymentId: string, etag: string, reasonCode: string, reason: string, token: string): Promise<ApiResult<{ payment: AdminPayment; provider_status: 'pending' | 'succeeded' | 'failed' | 'closed'; result: 'no_change' | 'status_updated'; reconciled_at: string }>> {
  return apiRequest(`/admin/payments/${encodeURIComponent(paymentId)}/reconciliations`, {
    method: 'POST',
    headers: { 'If-Match': etag, 'Idempotency-Key': createIdempotencyKey('admin-payment-reconcile') },
    body: JSON.stringify({ reason_code: reasonCode, reason }),
  }, token)
}
