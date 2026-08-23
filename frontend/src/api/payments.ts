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
  version: number
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
