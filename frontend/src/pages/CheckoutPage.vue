<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { formatMoney } from '@/api/catalog'
import { createOrder, getCheckout, listAddresses, patchCheckout, repriceCheckout, type AddressSummary, type CheckoutData } from '@/api/checkout'
import { createIdempotencyKey, errorMessage } from '@/api/http'
import { ensureStoreConversation, setConversationContext } from '@/api/messaging'
import PageState from '@/components/PageState.vue'
import { useUserAuthStore } from '@/stores/user-auth'

const route = useRoute(), router = useRouter(), auth = useUserAuthStore()
const checkout = ref<CheckoutData | null>(null), addresses = ref<AddressSummary[]>([])
const loading = ref(true), busy = ref(false), error = ref('')
const pendingOrderKey = ref('')
const remaining = computed(() => checkout.value ? new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(new Date(checkout.value.expires_at)) : '')
function token() { if (!auth.accessToken) throw new Error('missing token'); return auth.accessToken }
async function load() {
  loading.value = true; error.value = ''
  try { const [session, addressList] = await Promise.all([getCheckout(String(route.params.checkoutId), token()), listAddresses(token())]); checkout.value = session.data; addresses.value = addressList.data.items }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}
async function mutate(operation: () => Promise<{ data: CheckoutData }>) { busy.value = true; error.value = ''; try { checkout.value = (await operation()).data } catch (cause) { error.value = errorMessage(cause); await load() } finally { busy.value = false } }
function changeAddress(addressId: string) { if (!checkout.value) return; void mutate(() => patchCheckout(checkout.value!.checkout_id, { address_id: addressId }, checkout.value!.version, token())) }
function saveRemark(storeId: string, content: string) { if (!checkout.value) return; const remarks = checkout.value.store_groups.map((group) => ({ store_id: group.store_id, content: group.store_id === storeId ? content : group.buyer_remark || '' })); void mutate(() => patchCheckout(checkout.value!.checkout_id, { buyer_remarks: remarks }, checkout.value!.version, token())) }
async function submitOrder() {
  if (!checkout.value || !checkout.value.available_actions.includes('create_order')) return
  busy.value = true; error.value = ''
  if (!pendingOrderKey.value) pendingOrderKey.value = createIdempotencyKey('order-create')
  try {
    const response = await createOrder(checkout.value.checkout_id, checkout.value.version, token(), pendingOrderKey.value)
    pendingOrderKey.value = ''
    await router.push(`/pay/${response.data.trade_order_id}`)
  } catch (cause) { error.value = errorMessage(cause) }
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
  <section class="checkout-page">
    <header class="page-heading"><div><p class="eyebrow">确认交易信息</p><h1>结算</h1><p class="muted">会话有效至 {{ remaining }}，提交订单前仍会再次校验价格、库存和配送。</p></div><RouterLink to="/cart">返回购物车</RouterLink></header>
    <div v-if="error" class="notice error" role="alert">{{ error }}</div>
    <PageState :loading="loading" :error="''" :empty="false" @retry="load">
      <template v-if="checkout">
        <section class="checkout-card"><div class="section-heading"><div><p class="eyebrow">配送地址</p><h2>收货信息</h2></div><RouterLink to="/me/addresses">管理地址</RouterLink></div>
          <div v-if="addresses.length" class="address-choice-list"><label v-for="address in addresses" :key="address.address_id" :class="{ selected: checkout.address_id === address.address_id }"><input type="radio" name="address" :checked="checkout.address_id === address.address_id" :disabled="busy" @change="changeAddress(address.address_id)" /><span><strong>{{ address.recipient_name }} · {{ address.phone_masked }}</strong><small>{{ address.province_code }} {{ address.city_code }} {{ address.district_code }} {{ address.address }}</small></span></label></div>
          <p v-else class="notice warning">还没有收货地址。<RouterLink to="/me/addresses">新增地址</RouterLink></p>
        </section>
        <article v-for="group in checkout.store_groups" :key="group.store_id" class="checkout-card"><header><RouterLink :to="`/stores/${group.store_id}`"><strong>{{ group.store_name }}</strong> →</RouterLink><button type="button" class="secondary small" :disabled="busy" @click="contactStore(group.store_id)">联系商家</button></header>
          <div v-for="item in group.items" :key="item.sku_id" class="checkout-item-row"><div><RouterLink :to="`/products/${item.product_id}?sku_id=${item.sku_id}`"><strong>{{ item.product_name }}</strong></RouterLink><small>{{ item.sku_name }} · × {{ item.quantity }}</small></div><strong>{{ formatMoney(item.subtotal) }}</strong></div>
          <div class="delivery-summary"><span>配送方式：{{ group.delivery_options[0]?.name || '暂无可用配送' }}</span><strong>{{ formatMoney(group.freight_amount) }}</strong></div>
          <label class="remark-field">给商家留言（最多 200 字）<textarea maxlength="200" :value="group.buyer_remark || ''" :disabled="busy" @change="saveRemark(group.store_id, ($event.target as HTMLTextAreaElement).value)" /></label>
        </article>
        <section v-if="checkout.blocking_issues.length" class="notice error"><strong>暂时无法提交订单</strong><ul><li v-for="issue in checkout.blocking_issues" :key="`${issue.code}-${issue.sku_id}`">{{ issue.message }}</li></ul></section>
        <footer class="checkout-summary-bar"><button type="button" class="secondary" :disabled="busy" @click="mutate(() => repriceCheckout(checkout!.checkout_id, token()))">{{ busy ? '校验中…' : '刷新价格与库存' }}</button><dl><div><dt>商品金额</dt><dd>{{ formatMoney(checkout.amounts.goods_amount) }}</dd></div><div><dt>运费</dt><dd>{{ formatMoney(checkout.amounts.freight_amount) }}</dd></div><div class="total"><dt>应付</dt><dd>{{ formatMoney(checkout.amounts.payable_amount) }}</dd></div></dl><button type="button" :disabled="busy || !checkout.available_actions.includes('create_order')" @click="submitOrder">{{ busy ? '提交中…' : '提交订单' }}</button></footer>
      </template>
    </PageState>
  </section>
</template>
