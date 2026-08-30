<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { formatMoney } from '@/api/catalog'
import { createOrder, getCheckout, listAddresses, patchCheckout, repriceCheckout, type AddressSummary, type CheckoutData, type CheckoutItem } from '@/api/checkout'
import { ApiProblem, createIdempotencyKey, errorMessage, resolveApiAssetUrl } from '@/api/http'
import { ensureStoreConversation, setConversationContext } from '@/api/messaging'
import PageState from '@/components/PageState.vue'
import { useUserAuthStore } from '@/stores/user-auth'
import { formatChinaRegion } from '@/utils/china-regions'

const props = withDefaults(defineProps<{ checkoutId?: string; embedded?: boolean }>(), {
  checkoutId: '',
  embedded: false,
})
const emit = defineEmits<{ quantityChanged: [quantity: number]; cartChanged: [] }>()
const route = useRoute(), router = useRouter(), auth = useUserAuthStore()
const checkout = ref<CheckoutData | null>(null), addresses = ref<AddressSummary[]>([])
const loading = ref(true), busy = ref(false), error = ref('')
const pendingOrderKey = ref('')
const selectedAddressId = ref('')
const CURRENT_PRICING_POLICY = 'pricing_v2_free_shipping'
const requestedCheckoutId = computed(() => props.checkoutId || String(route.params.checkoutId || ''))
const remaining = computed(() => checkout.value ? new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(new Date(checkout.value.expires_at)) : '')
const buyNowItem = computed(() => checkout.value?.source_type === 'buy_now' ? checkout.value.store_groups[0]?.items[0] ?? null : null)
const checkoutQuantityLimit = computed(() => Math.max(1, Math.min(99, buyNowItem.value?.available_quantity ?? 1)))
function token() { if (!auth.accessToken) throw new Error('missing token'); return auth.accessToken }
async function load() {
  loading.value = true; error.value = ''
  try {
    const [session, addressList] = await Promise.all([getCheckout(requestedCheckoutId.value, token()), listAddresses(token())])
    addresses.value = addressList.data.items
    let current = session.data
    const selectedAddressExists = addresses.value.some((item) => item.address_id === current.address_id)
    if (addresses.value.length && (!current.address_id || !selectedAddressExists)) {
      const preferred = addresses.value.find((item) => item.is_default) ?? addresses.value[0]
      if (!preferred) throw new Error('收货地址列表读取失败，请稍后重试。')
      current = (await patchCheckout(current.checkout_id, { address_id: preferred.address_id }, current.version, token())).data
    } else if (
      current.pricing_version !== CURRENT_PRICING_POLICY
      || current.blocking_issues.some((item) => item.code === 'ADDRESS_REQUIRED' || item.code === 'DELIVERY_UNAVAILABLE')
    ) {
      current = (await repriceCheckout(current.checkout_id, token())).data
    }
    checkout.value = current
    selectedAddressId.value = current.address_id ?? ''
  }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}
async function mutate(operation: () => Promise<{ data: CheckoutData }>): Promise<boolean> { busy.value = true; error.value = ''; try { checkout.value = (await operation()).data; return true } catch (cause) { error.value = errorMessage(cause); await load(); return false } finally { busy.value = false } }
async function changeAddress(addressId: string) {
  if (!checkout.value || addressId === selectedAddressId.value) return
  selectedAddressId.value = addressId
  await mutate(() => patchCheckout(checkout.value!.checkout_id, { address_id: addressId }, checkout.value!.version, token()))
  selectedAddressId.value = checkout.value?.address_id ?? ''
}
function saveRemark(storeId: string, content: string) { if (!checkout.value) return; const remarks = checkout.value.store_groups.map((group) => ({ store_id: group.store_id, content: group.store_id === storeId ? content : group.buyer_remark || '' })); void mutate(() => patchCheckout(checkout.value!.checkout_id, { buyer_remarks: remarks }, checkout.value!.version, token())) }
async function changeQuantity(value: number) {
  if (!checkout.value || !buyNowItem.value || busy.value) return
  const nextQuantity = Math.min(checkoutQuantityLimit.value, Math.max(1, Math.trunc(value || 1)))
  if (nextQuantity === buyNowItem.value.quantity) return
  const changed = await mutate(() => patchCheckout(checkout.value!.checkout_id, { quantity: nextQuantity }, checkout.value!.version, token()))
  if (changed && buyNowItem.value) emit('quantityChanged', buyNowItem.value.quantity)
}
function itemQuantityLimit(item: CheckoutItem): number {
  return Math.max(1, Math.min(99, item.available_quantity || 1))
}
async function changeCartItemQuantity(item: CheckoutItem, value: number) {
  if (!checkout.value || !item.cart_item_id || checkout.value.source_type !== 'cart' || busy.value) return
  const nextQuantity = Math.min(itemQuantityLimit(item), Math.max(1, Math.trunc(value || 1)))
  if (nextQuantity === item.quantity) return
  const changed = await mutate(() => patchCheckout(
    checkout.value!.checkout_id,
    { item_quantities: [{ cart_item_id: item.cart_item_id!, quantity: nextQuantity }] },
    checkout.value!.version,
    token(),
  ))
  if (changed) emit('cartChanged')
}
async function submitOrder() {
  if (!checkout.value || !checkout.value.available_actions.includes('create_order')) return
  busy.value = true; error.value = ''
  if (!pendingOrderKey.value) pendingOrderKey.value = createIdempotencyKey('order-create')
  try {
    const response = await createOrder(checkout.value.checkout_id, checkout.value.version, token(), pendingOrderKey.value)
    pendingOrderKey.value = ''
    await router.push(`/pay/${response.data.trade_order_id}`)
  } catch (cause) {
    if (cause instanceof ApiProblem && cause.body.code === 'CHECKOUT_VERSION_MISMATCH') {
      try {
        checkout.value = (await repriceCheckout(checkout.value.checkout_id, token())).data
        selectedAddressId.value = checkout.value.address_id ?? ''
        if (checkout.value.available_actions.includes('create_order')) {
          const response = await createOrder(checkout.value.checkout_id, checkout.value.version, token(), pendingOrderKey.value)
          pendingOrderKey.value = ''
          await router.push(`/pay/${response.data.trade_order_id}`)
        }
      } catch (retryCause) { error.value = errorMessage(retryCause) }
    } else error.value = errorMessage(cause)
  }
  finally { busy.value = false }
}
async function contactStore(storeId: string) {
  if (!checkout.value || busy.value) return
  busy.value = true; error.value = ''
  try {
    const conversation = (await ensureStoreConversation(storeId, token())).data
    await setConversationContext(conversation.conversation_id, conversation.version, 'checkout_store_group', checkout.value.checkout_id, checkout.value.version, token())
    await router.push(`/messages/${conversation.conversation_id}`)
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}
onMounted(load)
</script>

<template>
  <section :class="['checkout-page', { 'checkout-page-embedded': embedded }]">
    <header class="page-heading"><div><p class="eyebrow">确认交易信息</p><h1>{{ embedded ? '确认订单' : '结算' }}</h1><p class="muted">会话有效至 {{ remaining }}，提交订单前仍会再次校验价格、库存和配送。</p></div><RouterLink v-if="!embedded" to="/cart">返回购物车</RouterLink></header>
    <div v-if="error" class="notice error" role="alert">{{ error }}</div>
    <PageState :loading="loading" :error="''" :empty="false" @retry="load">
      <template v-if="checkout">
        <div class="checkout-layout">
          <main class="checkout-main">
            <section class="checkout-card"><div class="section-heading"><div><p class="eyebrow">配送地址</p><h2>收货信息</h2></div><RouterLink to="/me/addresses">管理地址</RouterLink></div>
              <div v-if="addresses.length" class="address-choice-list"><label v-for="address in addresses" :key="address.address_id" :class="{ selected: selectedAddressId === address.address_id }"><input type="radio" name="address" :checked="selectedAddressId === address.address_id" :disabled="busy" @change="changeAddress(address.address_id)" /><span><strong>{{ address.recipient_name }} · {{ address.phone_masked }}</strong><small>{{ formatChinaRegion(address) }} {{ address.address }}</small></span></label></div>
              <p v-else class="notice warning">还没有收货地址。<RouterLink to="/me/addresses">新增地址</RouterLink></p>
            </section>
            <article v-for="group in checkout.store_groups" :key="group.store_id" class="checkout-card checkout-store-card"><header><RouterLink :to="`/stores/${group.store_id}`"><strong>{{ group.store_name }}</strong> →</RouterLink><button type="button" class="secondary small" :disabled="busy" @click="contactStore(group.store_id)">联系商家</button></header>
              <div v-for="item in group.items" :key="item.sku_id" :class="['checkout-item-row', { 'checkout-item-row-editable': embedded && checkout.source_type === 'cart' }]">
                <span class="checkout-item-thumb"><img v-if="item.image_url" :src="resolveApiAssetUrl(item.image_url) || undefined" :alt="`${item.sku_name}款式图`" /><b v-else aria-hidden="true">{{ item.product_name.slice(0, 1) }}</b></span>
                <div><RouterLink :to="`/products/${item.product_id}?sku_id=${item.sku_id}`"><strong>{{ item.product_name }}</strong></RouterLink><small>款式：{{ item.sku_name }}</small><small v-if="!embedded || checkout.source_type !== 'cart'">数量：{{ item.quantity }}</small>
                  <div v-else class="checkout-line-quantity"><span><b>数量</b><small>最多 {{ itemQuantityLimit(item) }} 件</small></span><span class="checkout-quantity-stepper"><button type="button" class="secondary" :disabled="busy || item.quantity <= 1" :aria-label="`减少${item.product_name}数量`" @click="changeCartItemQuantity(item, item.quantity - 1)">−</button><input :value="item.quantity" inputmode="numeric" :aria-label="`${item.product_name}结算数量`" :disabled="busy" @change="changeCartItemQuantity(item, Number(($event.target as HTMLInputElement).value))" /><button type="button" class="secondary" :disabled="busy || item.quantity >= itemQuantityLimit(item)" :aria-label="`增加${item.product_name}数量`" @click="changeCartItemQuantity(item, item.quantity + 1)">＋</button></span></div>
                </div>
                <strong>{{ formatMoney(item.subtotal) }}</strong>
              </div>
              <div class="delivery-summary"><span>配送方式：邮寄</span><strong class="free-shipping">包邮</strong></div>
              <details class="checkout-remark-details">
                <summary><span>给商家留言</span><small>{{ group.buyer_remark ? '已填写 · 点击修改' : '（更多）' }}</small></summary>
                <label class="remark-field"><span>留言内容（最多 200 字）</span><textarea maxlength="200" :value="group.buyer_remark || ''" :disabled="busy" placeholder="选填，可填写对发货或包装的说明" @change="saveRemark(group.store_id, ($event.target as HTMLTextAreaElement).value)" /></label>
              </details>
            </article>
            <section v-if="checkout.blocking_issues.length" class="notice error"><strong>暂时无法提交订单</strong><ul><li v-for="issue in checkout.blocking_issues" :key="`${issue.code}-${issue.sku_id}`">{{ issue.message }}</li></ul></section>
          </main>
          <aside class="checkout-summary-bar" aria-label="订单汇总">
            <div><p class="eyebrow">订单汇总</p><h2>确认金额</h2><small>共 {{ checkout.store_groups.reduce((total, group) => total + group.items.reduce((count, item) => count + item.quantity, 0), 0) }} 件商品</small></div>
            <template v-if="embedded && buyNowItem">
              <div class="checkout-embedded-quantity">
                <span><b>购买数量</b><small>最多可购 {{ checkoutQuantityLimit }} 件</small></span>
                <span class="checkout-quantity-stepper"><button type="button" class="secondary" :disabled="busy || buyNowItem.quantity <= 1" aria-label="减少结算数量" @click="changeQuantity(buyNowItem.quantity - 1)">−</button><input :value="buyNowItem.quantity" inputmode="numeric" aria-label="结算购买数量" :disabled="busy" @change="changeQuantity(Number(($event.target as HTMLInputElement).value))" /><button type="button" class="secondary" :disabled="busy || buyNowItem.quantity >= checkoutQuantityLimit" aria-label="增加结算数量" @click="changeQuantity(buyNowItem.quantity + 1)">＋</button></span>
              </div>
              <div class="checkout-embedded-total"><span><b>支付总额</b><small>邮寄包邮</small></span><strong>{{ formatMoney(checkout.amounts.payable_amount) }}</strong></div>
            </template>
            <dl v-else><div><dt>商品金额</dt><dd>{{ formatMoney(checkout.amounts.goods_amount) }}</dd></div><div><dt>运费</dt><dd class="free-shipping">包邮</dd></div><div class="total"><dt>应付</dt><dd>{{ formatMoney(checkout.amounts.payable_amount) }}</dd></div></dl>
            <button type="button" :disabled="busy || !checkout.available_actions.includes('create_order')" @click="submitOrder">{{ busy ? '提交中…' : '提交订单' }}</button>
            <p class="checkout-trust-note">提交即表示你已确认商品、款式、数量和收货信息。支付前不会扣款。</p>
          </aside>
        </div>
      </template>
    </PageState>
  </section>
</template>
