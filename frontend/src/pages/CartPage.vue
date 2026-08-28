<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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
  type CartStoreGroup,
} from '@/api/cart'
import { errorMessage, resolveApiAssetUrl } from '@/api/http'
import { createCartCheckout } from '@/api/checkout'
import PageState from '@/components/PageState.vue'
import CheckoutPage from '@/pages/CheckoutPage.vue'
import { useUserAuthStore } from '@/stores/user-auth'

const auth = useUserAuthStore()
const cart = ref<CartData | null>(null)
const loading = ref(true)
const busyItem = ref('')
const error = ref('')
const cartCheckoutId = ref('')
const cartCheckoutOpen = ref(false)
const cartCheckoutChanged = ref(false)
const checkoutDialog = ref<HTMLElement | null>(null)
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
    SKU_UNAVAILABLE: '当前款式不可用',
    INVENTORY_UNAVAILABLE: '暂时无法确认库存',
    INSUFFICIENT_STOCK: '库存不足，请调整数量',
  } as Record<string, string>)[reason || ''] || '商品当前不可结算'
}
function groupValidItems(group: CartStoreGroup): CartItem[] {
  return group.items.filter((item) => item.is_valid)
}
function groupSelected(group: CartStoreGroup): boolean {
  const items = groupValidItems(group)
  return items.length > 0 && items.every((item) => item.is_selected)
}
function toggleGroup(group: CartStoreGroup) {
  if (!cart.value) return
  const items = groupValidItems(group)
  if (!items.length) return
  void mutate(`store-${group.store_id}`, () => replaceCartSelection(
    items.map((item) => item.cart_item_id), !groupSelected(group), cart.value!.version, token(),
  ))
}
function itemSubtotal(item: CartItem): string {
  return formatMoney({
    minor_units: (BigInt(item.current_price.minor_units) * BigInt(item.quantity)).toString(),
    currency: item.current_price.currency,
  })
}
function quantityLimit(item: CartItem): number {
  return Math.max(1, Math.min(99, item.available_quantity || 1))
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
async function checkoutSelected() {
  if (!selectedIds.value.length) return
  busyItem.value = 'checkout'; error.value = ''
  try {
    const response = await createCartCheckout(selectedIds.value, token())
    cartCheckoutId.value = response.data.checkout_id
    cartCheckoutChanged.value = false
    cartCheckoutOpen.value = true
  }
  catch (cause) { error.value = errorMessage(cause) }
  finally { busyItem.value = '' }
}
function markCartCheckoutChanged() {
  cartCheckoutChanged.value = true
}
function closeCartCheckout() {
  cartCheckoutOpen.value = false
  if (cartCheckoutChanged.value) {
    cartCheckoutChanged.value = false
    void load()
  }
}
onMounted(load)
watch(cartCheckoutOpen, async (open) => {
  document.body.classList.toggle('modal-open', open)
  if (open) {
    await nextTick()
    checkoutDialog.value?.focus()
  }
})
onBeforeUnmount(() => document.body.classList.remove('modal-open'))
</script>

<template>
  <section class="cart-page">
    <header class="cart-hero"><div><p class="eyebrow">SHOPPING CART</p><h1>我的购物车</h1><p>核对商品、款式和数量，选中后即可统一结算。价格与库存会在下单前再次确认。</p></div><div class="cart-hero-summary"><strong>{{ cart?.cart_total_quantity ?? 0 }}</strong><span>件商品</span><RouterLink class="button-link" to="/search">继续购物</RouterLink></div></header>
    <div v-if="error" class="notice error" role="alert">{{ error }}</div>
    <PageState :loading="loading" :error="''" :empty="!cart || cart.groups.length === 0" empty-title="购物车还是空的" empty-message="去挑选喜欢的商品吧。" @retry="load">
      <template v-if="cart && cart.groups.length">
        <article v-for="group in cart.groups" :key="group.store_id" class="cart-store-card">
          <header>
            <div class="cart-store-heading"><input type="checkbox" :checked="groupSelected(group)" :disabled="busyItem !== '' || !groupValidItems(group).length" :aria-label="`选择${group.store_name}的全部有效商品`" @change="toggleGroup(group)" /><RouterLink class="cart-store-identity" :to="`/stores/${group.store_id}`"><span class="cart-store-logo"><img v-if="group.store_logo_url" :src="resolveApiAssetUrl(group.store_logo_url) || undefined" :alt="`${group.store_name} Logo`" /><b v-else>{{ group.store_name.slice(0, 1) }}</b></span><span><strong>{{ group.store_name }}</strong><small>进入店铺 →</small></span></RouterLink></div>
            <span class="cart-store-selected">本店已选 {{ group.selected_quantity }} 件 · <strong>{{ formatMoney(group.selected_amount) }}</strong></span>
          </header>
          <div class="cart-column-headings" aria-hidden="true"><span></span><span></span><span>商品信息</span><span>单价</span><span>数量</span><span>小计</span><span>操作</span></div>
          <div v-for="item in group.items" :key="item.cart_item_id" :class="['cart-item-row', { invalid: !item.is_valid }]">
            <input class="cart-item-select" type="checkbox" :checked="item.is_selected" :disabled="busyItem !== '' || !item.is_valid" :aria-label="`选择 ${item.product_name}`" @change="toggleItem(item)" />
            <RouterLink class="cart-product-image" :to="`/products/${item.product_id}?sku_id=${item.sku_id}`"><img v-if="item.image_url" :src="resolveApiAssetUrl(item.image_url) || undefined" :alt="`${item.product_name} ${item.sku_name}`" /><b v-else>{{ item.product_name.slice(0, 1) }}</b><span v-if="!item.is_valid">失效</span></RouterLink>
            <div class="cart-item-copy"><RouterLink :to="`/products/${item.product_id}?sku_id=${item.sku_id}`"><strong>{{ item.product_name }}</strong></RouterLink><span class="cart-sku-badge">款式：{{ item.sku_name }}</span><small v-if="!item.is_valid" class="error-text">{{ invalidReasonText(item.invalid_reason) }}</small><small v-else-if="item.price_changed" class="warning-text">价格已由 {{ formatMoney(item.added_price) }} 调整为 {{ formatMoney(item.current_price) }}</small><small v-else-if="item.available_quantity <= 5" class="warning-text">库存仅剩 {{ item.available_quantity }} 件</small><small v-else class="cart-stock-ok">库存充足</small></div>
            <strong class="cart-unit-price">{{ formatMoney(item.current_price) }}</strong>
            <div class="cart-quantity-stepper" aria-label="购买数量"><button type="button" class="secondary" :disabled="busyItem !== '' || item.quantity <= 1" aria-label="减少数量" @click="changeQuantity(item, item.quantity - 1)">−</button><input :value="item.quantity" type="number" min="1" :max="quantityLimit(item)" :disabled="busyItem !== ''" aria-label="数量" @change="changeQuantity(item, Number(($event.target as HTMLInputElement).value))" /><button type="button" class="secondary" :disabled="busyItem !== '' || item.quantity >= quantityLimit(item)" aria-label="增加数量" @click="changeQuantity(item, item.quantity + 1)">＋</button></div>
            <strong class="cart-line-total">{{ itemSubtotal(item) }}</strong>
            <button type="button" class="cart-remove link-button danger" :disabled="busyItem !== ''" @click="remove(item)">删除</button>
          </div>
        </article>
        <footer class="cart-summary-bar"><div class="cart-summary-actions"><button type="button" class="secondary" :disabled="busyItem !== '' || validItems.length === 0" @click="toggleAll">全选 / 取消全选</button><button v-if="hasInvalid" type="button" class="secondary" :disabled="busyItem !== ''" @click="clearInvalid">清理失效商品</button></div><div class="cart-checkout-summary"><span>已选 <b>{{ cart.selected_quantity }}</b> 件</span><span class="cart-total-copy">合计<small>全场包邮</small></span><strong>{{ formatMoney(cart.amount_summary.selected_goods_amount) }}</strong><button type="button" :disabled="busyItem !== '' || !selectedIds.length" @click="checkoutSelected">{{ busyItem === 'checkout' ? '创建结算…' : '去结算' }}</button></div></footer>
      </template>
    </PageState>
    <Teleport to="body">
      <div v-if="cartCheckoutOpen && cartCheckoutId" class="buy-now-checkout-overlay" @mousedown.self="closeCartCheckout" @keydown.esc="closeCartCheckout">
        <section ref="checkoutDialog" class="buy-now-checkout-dialog cart-checkout-dialog" role="dialog" aria-modal="true" aria-labelledby="cart-checkout-title" tabindex="-1">
          <header class="buy-now-checkout-dialog-header">
            <div><p class="eyebrow">购物车结算</p><h2 id="cart-checkout-title">确认所选商品</h2><p class="muted">可在下方分别调整每种商品的购买数量。</p></div>
            <button type="button" class="buy-now-checkout-close secondary" aria-label="关闭结算弹窗并返回购物车" @click="closeCartCheckout">×</button>
          </header>
          <CheckoutPage :checkout-id="cartCheckoutId" embedded @cart-changed="markCartCheckoutChanged" />
        </section>
      </div>
    </Teleport>
  </section>
</template>
