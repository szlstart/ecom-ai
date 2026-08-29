import { apiRequest, createIdempotencyKey, type ApiResult } from '@/api/http'
import type { Money } from '@/api/catalog'

export interface CartItem {
  cart_item_id: string
  product_id: string
  sku_id: string
  product_name: string
  sku_name: string
  image_url: string | null
  quantity: number
  is_selected: boolean
  added_price: Money
  current_price: Money
  price_changed: boolean
  available_quantity: number
  is_valid: boolean
  invalid_reason: string | null
}

export interface CartStoreGroup {
  store_id: string
  store_name: string
  store_logo_url: string | null
  items: CartItem[]
  selected_quantity: number
  selected_amount: Money
}

export interface CartData {
  cart_id: string | null
  groups: CartStoreGroup[]
  cart_total_quantity: number
  selected_quantity: number
  valid_item_count: number
  amount_summary: { selected_goods_amount: Money }
  version: number
}

export function getCart(token: string): Promise<ApiResult<CartData>> {
  return apiRequest('/users/me/cart', {}, token)
}

export function addCartItem(
  skuId: string,
  quantity: number,
  token: string,
  idempotencyKey = createIdempotencyKey('cart-add'),
): Promise<ApiResult<CartData>> {
  return apiRequest('/users/me/cart/items', {
    method: 'POST',
    headers: { 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify({ sku_id: skuId, quantity }),
  }, token)
}

export function patchCartItem(
  itemId: string,
  payload: { quantity?: number; is_selected?: boolean },
  version: number,
  token: string,
): Promise<ApiResult<CartData>> {
  return apiRequest(`/users/me/cart/items/${encodeURIComponent(itemId)}`, {
    method: 'PATCH', headers: { 'If-Match': `"v${version}"` }, body: JSON.stringify(payload),
  }, token)
}

export function deleteCartItem(
  itemId: string,
  version: number,
  token: string,
): Promise<ApiResult<CartData>> {
  return apiRequest(`/users/me/cart/items/${encodeURIComponent(itemId)}`, {
    method: 'DELETE', headers: { 'If-Match': `"v${version}"` },
  }, token)
}

export function replaceCartSelection(
  itemIds: string[],
  selected: boolean,
  version: number,
  token: string,
): Promise<ApiResult<CartData>> {
  return apiRequest('/users/me/cart/selection', {
    method: 'PUT',
    headers: { 'If-Match': `"v${version}"` },
    body: JSON.stringify({ cart_item_ids: itemIds, is_selected: selected }),
  }, token)
}

export function clearInvalidCartItems(
  version: number,
  token: string,
): Promise<ApiResult<CartData>> {
  return apiRequest('/users/me/cart/invalid-items', {
    method: 'DELETE', headers: { 'If-Match': `"v${version}"` },
  }, token)
}
