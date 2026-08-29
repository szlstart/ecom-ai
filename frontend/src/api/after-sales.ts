import { apiRequest, createIdempotencyKey, type ApiResult } from './http'
import type { Money } from './catalog'

export type RefundStatus = 'submitted' | 'merchant_review' | 'approved' | 'waiting_return' | 'returning' | 'received' | 'refunding' | 'succeeded' | 'rejected' | 'cancelled' | 'closed'

export interface RefundItem { order_item_id: string; quantity: number; requested_amount: Money }
export interface RefundApplication {
  refund_id: string
  order_id: string
  refund_type: 'refund_only' | 'return_and_refund'
  refund_status: RefundStatus
  reason_code: string
  reason_detail: string | null
  requested_amount: Money
  approved_amount: Money
  items: RefundItem[]
  available_actions: string[]
  submitted_at: string
  decided_at: string | null
  version: number
  claimed: boolean
}

export function listMyRefunds(token: string): Promise<ApiResult<{ items: RefundApplication[] }>> {
  return apiRequest('/users/me/refund-applications', {}, token)
}

export function getMyRefund(refundId: string, token: string): Promise<ApiResult<RefundApplication>> {
  return apiRequest(`/refund-applications/${encodeURIComponent(refundId)}`, {}, token)
}

export interface RefundEligibilityItem {
  order_item_id: string
  purchased_quantity: number
  succeeded_refund_quantity: number
  active_reserved_quantity: number
  available_quantity: number
  available_refundable_amount: Money
  available_actions: Array<'apply_after_sale' | 'view_active_after_sale'>
}

export interface RefundEligibility {
  eligible: boolean
  eligibility_token: string | null
  expires_at: string
  allowed_types: Array<'refund_only' | 'return_and_refund'>
  items: RefundEligibilityItem[]
  amount_editable: boolean
  min_refundable_amount: Money
  max_refundable_amount: Money
  suggested_refund_amount: Money
  blocking_reasons: string[]
}

export interface RefundSelection {
  order_item_id: string
  quantity: number
}

export interface RefundEligibilityInput {
  order_id: string
  items: RefundSelection[]
  requested_type: 'refund_only' | 'return_and_refund'
  reason_code: string
}

export function checkRefundEligibility(input: RefundEligibilityInput, token: string): Promise<ApiResult<RefundEligibility>> {
  return apiRequest('/refund-eligibility-checks', { method: 'POST', body: JSON.stringify(input) }, token)
}

export function createRefundApplication(input: RefundEligibilityInput, eligibility: RefundEligibility, reasonDetail: string | null, token: string): Promise<ApiResult<RefundApplication>> {
  if (!eligibility.eligibility_token) throw new Error('missing refund eligibility token')
  return apiRequest('/refund-applications', {
    method: 'POST',
    headers: { 'Idempotency-Key': createIdempotencyKey('refund-create') },
    body: JSON.stringify({
      eligibility_token: eligibility.eligibility_token,
      items: input.items,
      refund_type: input.requested_type,
      reason_code: input.reason_code,
      reason_detail: reasonDetail,
      requested_amount: eligibility.suggested_refund_amount,
      policy_accepted: true,
    }),
  }, token)
}

export interface RefundEvent {
  event_id: string
  from_status: string | null
  to_status: string
  event_code: string
  occurred_at: string
}

export function listRefundEvents(refundId: string, token: string): Promise<ApiResult<{ items: RefundEvent[] }>> {
  return apiRequest(`/refund-applications/${encodeURIComponent(refundId)}/events`, {}, token)
}

export function cancelRefund(refundId: string, reason: string | null, token: string): Promise<ApiResult<RefundApplication>> {
  return apiRequest(`/refund-applications/${encodeURIComponent(refundId)}/cancellations`, {
    method: 'POST',
    headers: { 'Idempotency-Key': createIdempotencyKey('refund-cancel') },
    body: JSON.stringify({ reason }),
  }, token)
}

export interface RefundAppeal {
  appeal_id: string
  refund_id: string
  appeal_status: 'submitted' | 'reviewing' | 'upheld' | 'rejected' | 'cancelled' | 'closed'
  reason: string
  submitted_at: string
  decided_at: string | null
  version: number
  claimed: boolean
}

export function createRefundAppeal(refundId: string, reason: string, token: string): Promise<ApiResult<RefundAppeal>> {
  return apiRequest(`/refund-applications/${encodeURIComponent(refundId)}/appeals`, {
    method: 'POST',
    headers: { 'Idempotency-Key': createIdempotencyKey('refund-appeal') },
    body: JSON.stringify({ reason }),
  }, token)
}

export function getRefundAppeal(appealId: string, token: string): Promise<ApiResult<RefundAppeal>> {
  return apiRequest(`/refund-appeals/${encodeURIComponent(appealId)}`, {}, token)
}

export interface ReturnShipmentInput { carrier_code: string; tracking_no: string }

export async function upsertReturnShipment(refundId: string, input: ReturnShipmentInput, version: number, token: string) {
  const response = await apiRequest<{ refund_id: string; carrier_code: string; carrier_name: string; tracking_no_masked: string; shipment_status: string; version: number }>(`/refund-applications/${refundId}/return-shipment`, {
    method: 'PUT',
    headers: { 'If-Match': `"v${version}"` },
    body: JSON.stringify(input),
  }, token)
  return response.data
}
