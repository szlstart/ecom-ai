import { apiRequest, createIdempotencyKey, type ApiResult } from '@/api/http'

export interface ReviewImage { file_id: string; width: number; height: number }
export interface MyReview {
  review_id: string; order_id: string; order_item_id: string; product_id: string; sku_id: string
  product_name: string; sku_name: string; rating: number; content: string | null; is_anonymous: boolean
  review_status: 'pending' | 'published' | 'hidden' | 'rejected'; moderation_status: 'pending' | 'passed' | 'blocked' | 'manual'
  images: ReviewImage[]; append: { append_id: string; content: string; append_status: string; moderation_status: string; images: ReviewImage[]; submitted_at: string; published_at: string | null } | null
  merchant_reply: { content: string; published_at: string } | null; submitted_at: string; published_at: string | null
  edit_deadline_at: string; append_deadline_at: string; available_actions: Array<'create' | 'view' | 'edit' | 'append'>; version: number
}
export interface ReviewEligibility { order_item_id: string; order_id: string; product_id: string; sku_id: string; product_name: string; sku_name: string; order_completed_at: string | null; review_deadline_at: string | null; eligible: boolean; reason_code: string | null; reason_message: string | null; existing_review_id: string | null; available_actions: Array<'create' | 'view' | 'edit' | 'append'> }
export interface MyReviewListItem { item_type: 'pending' | 'review'; order_id: string; order_item_id: string; product_id: string; sku_id: string; product_name: string; sku_name: string; order_completed_at: string | null; eligibility: ReviewEligibility; review: MyReview | null }
export interface ReviewInput { rating: number; content: string | null; is_anonymous: boolean; image_file_ids: string[] }

export function listMyReviews(view: 'pending' | 'published', token: string): Promise<ApiResult<{ items: MyReviewListItem[] }>> { return apiRequest(`/users/me/reviews?view=${view}`, {}, token) }
export function getReviewEligibility(orderItemId: string, token: string): Promise<ApiResult<ReviewEligibility>> { return apiRequest(`/review-eligibilities/${encodeURIComponent(orderItemId)}`, {}, token) }
export function getMyReview(reviewId: string, token: string): Promise<ApiResult<MyReview>> { return apiRequest(`/reviews/${encodeURIComponent(reviewId)}`, {}, token) }
export function createReview(orderItemId: string, input: ReviewInput, token: string): Promise<ApiResult<MyReview>> { return apiRequest('/reviews', { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('review-create') }, body: JSON.stringify({ order_item_id: orderItemId, ...input }) }, token) }
export function updateReview(reviewId: string, version: number, input: ReviewInput, token: string): Promise<ApiResult<MyReview>> { return apiRequest(`/reviews/${encodeURIComponent(reviewId)}`, { method: 'PATCH', headers: { 'If-Match': `"v${version}"` }, body: JSON.stringify(input) }, token) }
export function appendReview(reviewId: string, content: string, imageFileIds: string[], token: string): Promise<ApiResult<MyReview>> { return apiRequest(`/reviews/${encodeURIComponent(reviewId)}/append-records`, { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('review-append') }, body: JSON.stringify({ content, image_file_ids: imageFileIds }) }, token) }
