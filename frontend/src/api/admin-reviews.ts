import { apiRequest, createIdempotencyKey, type ApiResult } from '@/api/http'

export interface AdminReview {
  review_id: string
  order_id: string
  order_item_id: string
  user_id: string
  user_name: string
  store_id: string
  store_name: string
  product_id: string
  product_name: string
  sku_id: string
  sku_name: string
  rating: number
  content: string | null
  is_anonymous: boolean
  review_status: 'pending' | 'published' | 'hidden' | 'rejected'
  moderation_status: 'pending' | 'passed' | 'blocked' | 'manual'
  merchant_reply: { content: string; published_at: string } | null
  governance_history: Array<{ governance_id: string; action: 'hide' | 'restore'; from_status: string; to_status: string; rule_code: string; reason: string; occurred_at: string }>
  submitted_at: string
  published_at: string | null
  version: number
}

export function listAdminReviews(token: string, status?: string, productId?: string): Promise<ApiResult<{ items: AdminReview[] }>> {
  const query = new URLSearchParams()
  if (status) query.set('review_status', status)
  if (productId) query.set('product_id', productId)
  const suffix = query.size ? `?${query}` : ''
  return apiRequest(`/admin/reviews${suffix}`, {}, token)
}

export function getAdminReview(reviewId: string, token: string): Promise<ApiResult<AdminReview>> {
  return apiRequest(`/admin/reviews/${encodeURIComponent(reviewId)}`, {}, token)
}

export function replyAdminReview(reviewId: string, etag: string, content: string, token: string): Promise<ApiResult<AdminReview>> {
  return apiRequest(`/admin/reviews/${encodeURIComponent(reviewId)}/replies`, {
    method: 'POST',
    headers: { 'If-Match': etag, 'Idempotency-Key': createIdempotencyKey('review-reply') },
    body: JSON.stringify({ content }),
  }, token)
}

export function moderateAdminReview(reviewId: string, etag: string, action: 'hide' | 'restore', ruleCode: string, reason: string, token: string): Promise<ApiResult<AdminReview>> {
  return apiRequest(`/admin/reviews/${encodeURIComponent(reviewId)}/moderations`, {
    method: 'POST',
    headers: { 'If-Match': etag, 'Idempotency-Key': createIdempotencyKey(`review-${action}`) },
    body: JSON.stringify({ action, rule_code: ruleCode, reason }),
  }, token)
}
