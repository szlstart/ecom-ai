import { apiRequest, type ApiResult } from '@/api/http'

export interface Money {
  minor_units: string
  currency: string
}

export interface PublicImage {
  file_id: string
  url: string
  thumbnail_url: string
  alt_text: string | null
  width: number
  height: number
  sort_order: number
}

export interface ProductCardData {
  product_id: string
  store_id: string
  store_name: string
  product_name: string
  subtitle: string | null
  price: Money
  price_range: Money | null
  sales_count: number
  rating_score: string
  main_image: PublicImage | null
  is_favorited: boolean
}

export interface Category {
  category_id: string
  parent_id: string | null
  category_name: string
  category_code: string
  level: number
  sort_order: number
  icon_url: string | null
  children: Category[]
}

export interface Brand {
  brand_id: string
  brand_name: string
  logo_url: string | null
  description: string | null
}

export interface SafeContent {
  content_format: 'structured_v1' | 'safe_html_v1'
  content_version: number
  content_hash: string
  safe_blocks: Array<Record<string, unknown>> | null
  safe_html: string | null
  safe_text_fallback: string
}

export interface ServiceEstimate {
  estimate_type: 'dispatch' | 'delivery'
  status: 'available' | 'unavailable'
  min_at: string | null
  max_at: string | null
  source: string | null
  source_updated_at: string | null
  calculated_at: string
  timezone: string
  disclaimer_code: string | null
  unavailable_reason_code: string | null
}

export interface StoreSummary {
  store_id: string
  store_name: string
  logo_url: string | null
  store_status: string
  rating_score: string
}

export interface ProductDetailData {
  product_id: string
  product_name: string
  subtitle: string | null
  description: string | null
  product_status: string
  category_id: string
  brand_id: string | null
  store: StoreSummary
  price_range: [Money, Money]
  sales_count: number
  review_count: number
  rating_score: string
  public_images: PublicImage[]
  default_sku_id: string | null
  detail_content: SafeContent | null
  attributes: Array<Record<string, string | boolean | null>>
  origin_region_code: string | null
  dispatch_estimate: ServiceEstimate
  purchase_notice: string | null
  fulfillment_profile_version: number | null
  is_favorited: boolean
}

export interface ProductSku {
  sku_id: string
  sku_name: string
  spec_values: Array<Record<string, string>>
  sale_price: Money
  market_price: Money
  sku_status: string
  stock_status: 'in_stock' | 'low_stock' | 'out_of_stock' | 'frozen'
  low_stock_remaining: number | null
  max_purchase_quantity: number
  sales_count: number
  images: PublicImage[]
  image_fallback: 'none' | 'product_public_images'
}

export interface ProductFaq {
  faq_id: string
  question: string
  answer_content: SafeContent
}

export interface HomepageSection {
  section: 'recommended' | 'hot' | 'new_arrival'
  title: string
  status: 'available' | 'unavailable'
  items: ProductCardData[]
  next_cursor: string | null
  error_code: string | null
}

export interface HomepageData {
  feed_version: string
  announcements: Array<Record<string, string>>
  banners: Array<Record<string, unknown>>
  categories: Category[]
  sections: HomepageSection[]
}

export interface StoreData {
  store_id: string
  store_name: string
  logo_url: string | null
  description: string | null
  store_status: string
  visibility_mode: string
  rating_score: string
  rating_count: number
  follower_count: number
  sales_count: number
  opened_at: string | null
  active_product_count: number
  is_followed: boolean
  customer_service_enabled: boolean
}

export interface StoreGroup {
  group_id: string
  group_name: string
  sort_order: number
  visible_product_count: number
  children: StoreGroup[]
}

export interface StorePolicy {
  policy_id: string
  policy_type: string
  title: string
  content: string
  policy_version: number
  effective_at: string
  expires_at: string | null
}

export interface StoreHomeContent {
  announcements: Array<Record<string, string>>
  recommended_products: ProductCardData[]
  hot_products: ProductCardData[]
}

export interface ReviewImage {
  file_id: string
  url: string
  thumbnail_url: string
  width: number
  height: number
}

export interface ProductReview {
  review_id: string
  user_display_name: string
  sku_id: string
  sku_name: string
  rating: number
  content: string | null
  published_at: string
  helpful_count: number
  images: ReviewImage[]
  append: { content: string; published_at: string } | null
  merchant_reply: { content: string; published_at: string } | null
}

export interface ProductReviewList {
  summary: {
    review_count: number
    average_rating: string
    rating_distribution: Record<string, number>
    image_review_count: number
  }
  items: ProductReview[]
}

export interface ProductSearchFilters {
  q?: string
  category_id?: string
  brand_id?: string
  store_id?: string
  group_id?: string
  price_min?: string
  price_max?: string
  sort?: string
  cursor?: string
  limit?: number
}

export function getHomepage(accessToken?: string | null): Promise<ApiResult<HomepageData>> {
  return apiRequest('/homepage', {}, accessToken)
}

export function searchProducts(
  filters: ProductSearchFilters,
  accessToken?: string | null,
): Promise<ApiResult<{ items: ProductCardData[] }>> {
  return apiRequest(`/products?${query({
    q: filters.q,
    category_id: filters.category_id,
    brand_id: filters.brand_id,
    store_id: filters.store_id,
    price_min: filters.price_min,
    price_max: filters.price_max,
    sort: filters.sort,
    cursor: filters.cursor,
    limit: filters.limit,
  })}`, {}, accessToken)
}

export function getProduct(
  productId: string,
  accessToken?: string | null,
): Promise<ApiResult<ProductDetailData>> {
  return apiRequest(`/products/${encodeURIComponent(productId)}`, {}, accessToken)
}

export function getProductSkus(productId: string): Promise<ApiResult<{ items: ProductSku[] }>> {
  return apiRequest(`/products/${encodeURIComponent(productId)}/skus`)
}

export function getProductFaqs(productId: string): Promise<ApiResult<{ items: ProductFaq[] }>> {
  return apiRequest(`/products/${encodeURIComponent(productId)}/faqs`)
}

export function getProductReviews(
  productId: string,
  filters: { rating?: string; has_image?: string; sku_id?: string; sort?: string; cursor?: string },
): Promise<ApiResult<ProductReviewList>> {
  return apiRequest(`/products/${encodeURIComponent(productId)}/reviews?${query(filters)}`)
}

export function getCategories(): Promise<ApiResult<Category[]>> {
  return apiRequest('/categories')
}

export function getBrands(q?: string): Promise<ApiResult<Brand[]>> {
  return apiRequest(`/brands?${query({ q, limit: 100 })}`)
}

export function getSuggestions(q: string): Promise<ApiResult<{ items: string[] }>> {
  return apiRequest(`/search/suggestions?${query({ q, limit: 8 })}`)
}

export function getFavoriteProducts(
  accessToken: string,
): Promise<ApiResult<{ items: ProductCardData[] }>> {
  return apiRequest('/users/me/favorite-products?limit=50', {}, accessToken)
}

export function setProductFavorite(
  productId: string,
  favorite: boolean,
  accessToken: string,
): Promise<ApiResult<void>> {
  return apiRequest(
    `/users/me/favorite-products/${encodeURIComponent(productId)}`,
    { method: favorite ? 'PUT' : 'DELETE' },
    accessToken,
  )
}

export function getStore(
  storeId: string,
  accessToken?: string | null,
): Promise<ApiResult<StoreData>> {
  return apiRequest(`/stores/${encodeURIComponent(storeId)}`, {}, accessToken)
}

export function getStoreProducts(
  storeId: string,
  filters: ProductSearchFilters,
  accessToken?: string | null,
): Promise<ApiResult<{ items: ProductCardData[] }>> {
  const allowed = {
    q: filters.q,
    group_id: filters.group_id,
    sort: filters.sort,
    cursor: filters.cursor,
    limit: filters.limit,
  }
  return apiRequest(`/stores/${encodeURIComponent(storeId)}/products?${query(allowed)}`, {}, accessToken)
}

export function getStoreGroups(storeId: string): Promise<ApiResult<{ items: StoreGroup[] }>> {
  return apiRequest(`/stores/${encodeURIComponent(storeId)}/product-groups`)
}

export function getStoreHome(
  storeId: string,
  accessToken?: string | null,
): Promise<ApiResult<StoreHomeContent>> {
  return apiRequest(`/stores/${encodeURIComponent(storeId)}/home-content`, {}, accessToken)
}

export function getStorePolicies(storeId: string): Promise<ApiResult<{ items: StorePolicy[] }>> {
  return apiRequest(`/stores/${encodeURIComponent(storeId)}/service-policies`)
}

export function getFollowedStores(accessToken: string): Promise<ApiResult<{ items: StoreData[] }>> {
  return apiRequest('/users/me/followed-stores?limit=50', {}, accessToken)
}

export function setStoreFollow(
  storeId: string,
  followed: boolean,
  accessToken: string,
): Promise<ApiResult<void>> {
  return apiRequest(
    `/users/me/followed-stores/${encodeURIComponent(storeId)}`,
    { method: followed ? 'PUT' : 'DELETE' },
    accessToken,
  )
}

function query(values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== '') params.set(key, String(value))
  }
  return params.toString()
}

export function formatMoney(money: Money): string {
  const value = Number(money.minor_units) / 100
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency: money.currency,
    minimumFractionDigits: 2,
  }).format(value)
}
