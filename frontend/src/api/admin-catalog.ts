import { apiRequest, createIdempotencyKey, type ApiResult } from '@/api/http'

export interface AdminVersioned {
  version: number
}

export interface AdminCategory extends AdminVersioned {
  category_id: string
  parent_id: string | null
  category_name: string
  category_code: string
  path: string
  level: number
  sort_order: number
  icon_url: string | null
  status: string
}

export interface AdminBrand extends AdminVersioned {
  brand_id: string
  brand_name: string
  logo_url: string | null
  description: string | null
  status: string
}

export interface AdminInventory extends AdminVersioned {
  sku_id: string
  sku_name: string
  product_id: string
  product_name: string
  store_id: string
  store_name: string
  on_hand_quantity: number
  reserved_quantity: number
  safety_stock_quantity: number
  available_quantity: number
  sold_quantity: number
  status: string
  last_reconciled_at: string | null
}

export interface AdminStore extends AdminVersioned {
  store_id: string
  owner_user_id: string
  store_name: string
  description: string | null
  logo_file_id: string | null
  logo_url: string | null
  status: string
  rating_score: string
  rating_count: number
  follower_count: number
  sales_count: number
  store_name_changed_at: string | null
  store_name_change_available_at: string | null
  opened_at: string | null
  suspended_at: string | null
  closed_at: string | null
}

export interface AdminStorePolicy extends AdminVersioned {
  policy_id: string
  store_id: string
  policy_type: string
  title: string
  content: string
  policy_version: number
  status: string
  effective_at: string | null
  expires_at: string | null
  published_at: string | null
  withdrawn_at: string | null
}

export interface AdminStoreGroup extends AdminVersioned {
  group_id: string
  store_id: string
  parent_group_id: string | null
  group_name: string
  status: string
  sort_order: number
  product_ids: string[]
}

export interface AdminShippingRule {
  region_scope: Record<string, unknown>
  first_unit: number
  additional_unit: number
  first_fee_amount: number
  additional_fee_amount: number
  estimated_min_days: number | null
  estimated_max_days: number | null
}

export interface AdminShippingTemplate extends AdminVersioned {
  template_id: string
  template_family_id: string
  store_id: string
  template_name: string
  delivery_type: string
  charge_mode: string
  currency: string
  status: string
  dispatch_min_hours: number
  dispatch_max_hours: number
  policy_version: number
  rules: AdminShippingRule[]
}

export interface AdminStoreAnnouncement extends AdminVersioned {
  announcement_id: string
  store_id: string
  title: string
  content: string
  status: string
  starts_at: string | null
  ends_at: string | null
  sort_order: number
}

export interface AdminFeaturedProduct {
  product_id: string
  starts_at: string | null
  ends_at: string | null
  slot_type: string
  sort_order: number
}

export interface AdminProductSummary extends AdminVersioned {
  product_id: string
  store_id: string
  store_name: string
  category_id: string
  category_name: string
  brand_id: string | null
  brand_name: string | null
  product_name: string
  subtitle: string | null
  status: string
  min_price: string
  max_price: string
  currency: string
  cover_image_url: string | null
  sku_count: number
  available_quantity: number
  sales_count: number
  review_count: number
  rating_score: string
  updated_at: string
}

export interface AdminProduct extends AdminProductSummary {
  description: string | null
  default_sku_id: string | null
  current_detail_content_version_id: string | null
  published_detail_content_version_id: string | null
  completeness: {
    basic: boolean
    sku: boolean
    main_image: boolean
    attributes: boolean
    fulfillment: boolean
    detail_content: boolean
    missing_requirements: string[]
  }
  available_actions: string[]
  published_at: string | null
  off_shelf_at: string | null
}

export interface AdminProductDeletionEligibility {
  product_id: string
  current_status: string
  has_transactions: boolean
  can_delete: boolean
  can_off_shelf: boolean
  recommended_action: 'delete' | 'off_shelf' | 'none'
  message: string
}

export interface AdminSku extends AdminVersioned {
  sku_id: string
  product_id: string
  merchant_sku_code: string | null
  sku_name: string
  spec_values: Array<{ name: string; value: string }>
  sale_price: string
  market_price: string
  currency: string
  weight_grams: number | null
  barcode: string | null
  status: string
}

export interface AdminProductImage {
  file_id: string
  sku_id: string
  image_type: 'spec'
  alt_text: string | null
  sort_order: number
  image_url: string
  width: number
  height: number
  status: string
}

export interface AdminProductAttribute {
  attribute_code: string
  attribute_name: string
  value_text: string
  value_normalized: string | null
  unit: string | null
  is_searchable: boolean
  sort_order: number
}

export interface AdminProductFaq extends AdminVersioned {
  faq_id: string
  product_id: string
  question: string
  status: string
  sort_order: number
  current_version_id: string | null
  current_answer_text: string | null
  published_version_id: string | null
  published_at: string | null
}

export interface AdminContentVersion {
  version_id: string
  content_version: number
  source_format: string
  source_content: string
  public_content_format: string
  safe_blocks: Array<Record<string, unknown>> | null
  safe_html: string | null
  safe_text: string
  security_scan_status: string
  status: string
  created_at: string
}

export interface AdminBatchJob extends AdminVersioned {
  job_id: string
  job_type: string
  store_id: string
  schema_version: string
  status: string
  total_count: number
  success_count: number
  failure_count: number
  preview_hash: string | null
  input_file_id: string | null
  result_file_id: string | null
  error_file_id: string | null
  error_code: string | null
  error_summary: string | null
  requested_at: string
  started_at: string | null
  finished_at: string | null
  expires_at: string | null
  available_actions: string[]
}

export interface AdminBatchJobItem {
  item_key: string
  item_status: string
  resource_type: string | null
  resource_id: string | null
  error_code: string | null
  error_message: string | null
}

export interface ProductImportTemplate {
  schema_version: string
  supported_file_types: string[]
  maximum_rows: number
  columns: Array<{ name: string; required: boolean; description: string; example: string }>
}

export function adminQuery(values: Record<string, string | number | null | undefined>): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  }
  const encoded = params.toString()
  return encoded ? `?${encoded}` : ''
}

export function versionEtag(version: number): string {
  return `"v${version}"`
}

export function adminGet<T>(path: string, token: string): Promise<ApiResult<T>> {
  return apiRequest(path, {}, token)
}

export function adminCreate<T>(
  path: string,
  payload: unknown,
  token: string,
  keyPrefix: string,
): Promise<ApiResult<T>> {
  return apiRequest(
    path,
    {
      method: 'POST',
      headers: { 'Idempotency-Key': createIdempotencyKey(keyPrefix) },
      body: JSON.stringify(payload),
    },
    token,
  )
}

export function adminReplace<T>(
  path: string,
  payload: unknown,
  token: string,
  version: number,
): Promise<ApiResult<T>> {
  return apiRequest(
    path,
    {
      method: 'PUT',
      headers: { 'If-Match': versionEtag(version) },
      body: JSON.stringify(payload),
    },
    token,
  )
}

export function adminUpdate<T>(
  path: string,
  payload: unknown,
  token: string,
  version: number,
): Promise<ApiResult<T>> {
  return apiRequest(
    path,
    {
      method: 'PATCH',
      headers: { 'If-Match': versionEtag(version) },
      body: JSON.stringify(payload),
    },
    token,
  )
}

export function adminDelete<T>(
  path: string,
  token: string,
  version: number,
  keyPrefix: string,
): Promise<ApiResult<T>> {
  return apiRequest(
    path,
    {
      method: 'DELETE',
      headers: {
        'If-Match': versionEtag(version),
        'Idempotency-Key': createIdempotencyKey(keyPrefix),
      },
    },
    token,
  )
}

export function adminCommand<T>(
  path: string,
  payload: unknown,
  token: string,
  version: number,
  keyPrefix: string,
): Promise<ApiResult<T>> {
  return apiRequest(
    path,
    {
      method: 'POST',
      headers: {
        'If-Match': versionEtag(version),
        'Idempotency-Key': createIdempotencyKey(keyPrefix),
      },
      body: JSON.stringify(payload),
    },
    token,
  )
}

export function requireAdminToken(token: string | null): string {
  if (!token) throw new Error('管理端会话不可用')
  return token
}
