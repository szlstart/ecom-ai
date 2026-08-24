import type { RefundAppeal, RefundApplication } from '@/api/after-sales'
import { apiRequest, createIdempotencyKey, type ApiResult } from '@/api/http'

export function listAdminRefunds(token: string): Promise<ApiResult<{ items: RefundApplication[] }>> {
  return apiRequest('/admin/refund-applications', {}, token)
}

export function getAdminRefund(refundId: string, token: string): Promise<ApiResult<RefundApplication>> {
  return apiRequest(`/admin/refund-applications/${encodeURIComponent(refundId)}`, {}, token)
}

export function claimAdminRefund(refundId: string, etag: string, token: string): Promise<ApiResult<RefundApplication>> {
  return apiRequest(`/admin/refund-applications/${encodeURIComponent(refundId)}/claims`, { method: 'POST', headers: { 'If-Match': etag } }, token)
}

export function decideAdminRefund(refundId: string, etag: string, decision: 'approve' | 'reject', reasonCode: string, reason: string, token: string): Promise<ApiResult<RefundApplication>> {
  return apiRequest(`/admin/refund-applications/${encodeURIComponent(refundId)}/decisions`, {
    method: 'POST',
    headers: { 'If-Match': etag },
    body: JSON.stringify({ decision, reason_code: reasonCode, reason }),
  }, token)
}

export function listAdminAppeals(token: string): Promise<ApiResult<{ items: RefundAppeal[] }>> {
  return apiRequest('/admin/refund-appeals', {}, token)
}

export function getAdminAppeal(appealId: string, token: string): Promise<ApiResult<RefundAppeal>> {
  return apiRequest(`/admin/refund-appeals/${encodeURIComponent(appealId)}`, {}, token)
}

export function claimAdminAppeal(appealId: string, etag: string, token: string): Promise<ApiResult<RefundAppeal>> {
  return apiRequest(`/admin/refund-appeals/${encodeURIComponent(appealId)}/claims`, { method: 'POST', headers: { 'If-Match': etag } }, token)
}

export function decideAdminAppeal(appealId: string, etag: string, decision: 'approve' | 'reject', reason: string, token: string): Promise<ApiResult<RefundAppeal>> {
  return apiRequest(`/admin/refund-appeals/${encodeURIComponent(appealId)}/decisions`, {
    method: 'POST',
    headers: { 'If-Match': etag, 'Idempotency-Key': createIdempotencyKey('appeal-decision') },
    body: JSON.stringify({ decision, reason }),
  }, token)
}
