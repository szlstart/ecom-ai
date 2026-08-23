<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { formatMoney } from '@/api/catalog'
import {
  clearInvalidCartItems,
  deleteCartItem,
  getCart,
  patchCartItem,
  replaceCartSelection,
  type CartData,
  type CartItem,
} from '@/api/cart'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useUserAuthStore } from '@/stores/user-auth'

const auth = useUserAuthStore()
const cart = ref<CartData | null>(null)
const loading = ref(true)
const busyItem = ref('')
const error = ref('')
const selectedIds = computed(() => cart.value?.groups.flatMap((group) => group.items)
  .filter((item) => item.is_selected).map((item) => item.cart_item_id) ?? [])
const validItems = computed(() => cart.value?.groups.flatMap((group) => group.items)
  .filter((item) => item.is_valid) ?? [])
const hasInvalid = computed(() => cart.value?.groups.some((group) => group.items.some((item) => !item.is_valid)) ?? false)

function token(): string {
  if (!auth.accessToken) throw new Error('missing user token')
  return auth.accessToken
}
function invalidReasonText(reason: string | null): string {
  return ({
    STORE_UNAVAILABLE: '店铺当前不可用',
    PRODUCT_OFF_SHELF: '商品已下架',
    SKU_UNAVAILABLE: '当前规格不可用',
    INVENTORY_UNAVAILABLE: '暂时无法确认库存',
    INSUFFICIENT_STOCK: '库存不足，请调整数量',
  } as Record<string, string>)[reason || ''] || '商品当前不可结算'
}
async function load() {
  loading.value = true; error.value = ''
  try { cart.value = (await getCart(token())).data }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}
async function mutate(itemId: string, operation: () => Promise<{ data: CartData }>) {
  busyItem.value = itemId; error.value = ''
  try { cart.value = (await operation()).data }
  catch (cause) { error.value = errorMessage(cause); await load() }
  finally { busyItem.value = '' }
}
function changeQuantity(item: CartItem, value: number) {
  if (!cart.value) return
  const quantity = Math.min(99, Math.max(1, Math.trunc(value || 1)))
  void mutate(item.cart_item_id, () => patchCartItem(item.cart_item_id, { quantity }, cart.value!.version, token()))
}
function toggleItem(item: CartItem) {
  if (!cart.value) return
  void mutate(item.cart_item_id, () => patchCartItem(item.cart_item_id, { is_selected: !item.is_selected }, cart.value!.version, token()))
}
function remove(item: CartItem) {
  if (!cart.value) return
  void mutate(item.cart_item_id, () => deleteCartItem(item.cart_item_id, cart.value!.version, token()))
}
function toggleAll() {
  if (!cart.value || validItems.value.length === 0) return
  const allSelected = validItems.value.every((item) => item.is_selected)
  void mutate('selection', () => replaceCartSelection(validItems.value.map((item) => item.cart_item_id), !allSelected, cart.value!.version, token()))
}
function clearInvalid() {
  if (!cart.value) return
  void mutate('invalid', () => clearInvalidCartItems(cart.value!.version, token()))
}
onMounted(load)
</script>

<template>
  <section class="cart-page">
    <header class="page-heading"><div><p class="eyebrow">购物袋</p><h1>购物车</h1><p class="muted">商品价格、库存和配送资格会在结算及下单时再次校验。</p></div><RouterLink to="/search">继续购物</RouterLink></header>
    <div v-if="error" class="notice error" role="alert">{{ error }}</div>
    <PageState :loading="loading" :error="''" :empty="!cart || cart.groups.length === 0" empty-title="购物车还是空的" empty-message="去挑选喜欢的商品吧。" @retry="load">
      <template v-if="cart && cart.groups.length">
        <article v-for="group in cart.groups" :key="group.store_id" class="cart-store-card">
          <header><RouterLink :to="`/stores/${group.store_id}`"><strong>{{ group.store_name }}</strong> →</RouterLink><span>已选 {{ group.selected_quantity }} 件 · {{ formatMoney(group.selected_amount) }}</span></header>
          <div v-for="item in group.items" :key="item.cart_item_id" :class="['cart-item-row', { invalid: !item.is_valid }]">
            <input type="checkbox" :checked="item.is_selected" :disabled="busyItem !== '' || !item.is_valid" :aria-label="`选择 ${item.product_name}`" @change="toggleItem(item)" />
            <div class="cart-item-copy"><RouterLink :to="`/products/${item.product_id}?sku_id=${item.sku_id}`"><strong>{{ item.product_name }}</strong></RouterLink><small>{{ item.sku_name }} · {{ item.spec_values.map((spec) => `${spec.name}:${spec.value}`).join(' / ') }}</small><small v-if="!item.is_valid" class="error-text">{{ invalidReasonText(item.invalid_reason) }}</small><small v-else-if="item.price_changed" class="warning-text">价格已由 {{ formatMoney(item.added_price) }} 变为 {{ formatMoney(item.current_price) }}</small></div>
            <strong>{{ formatMoney(item.current_price) }}</strong>
            <label>数量<input :value="item.quantity" type="number" min="1" max="99" :disabled="busyItem !== ''" @change="changeQuantity(item, Number(($event.target as HTMLInputElement).value))" /></label>
            <button type="button" class="link-button danger" :disabled="busyItem !== ''" @click="remove(item)">删除</button>
          </div>
        </article>
        <footer class="cart-summary-bar"><div class="actions"><button type="button" class="secondary" :disabled="busyItem !== '' || validItems.length === 0" @click="toggleAll">全选/取消全选</button><button v-if="hasInvalid" type="button" class="secondary" :disabled="busyItem !== ''" @click="clearInvalid">清理失效商品</button></div><div><span>已选 {{ cart.selected_quantity }} 件</span><strong>{{ formatMoney(cart.amount_summary.selected_goods_amount) }}</strong><button type="button" disabled :title="selectedIds.length ? '结算切片接入后开放' : '请先选择有效商品'">去结算</button></div></footer>
      </template>
    </PageState>
  </section>
</template>
