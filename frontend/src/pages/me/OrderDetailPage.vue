<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { formatMoney } from '@/api/catalog'
import { getCart } from '@/api/cart'
import { errorMessage, resolveApiAssetUrl } from '@/api/http'
import { ensureStoreConversation, setConversationContext } from '@/api/messaging'
import { cancelOrder, confirmOrderReceipt, getMyOrder, hideOrder, repurchaseOrder, restoreOrder, type OrderAction, type OrderDetail, type OrderHideResult } from '@/api/orders'
import PageState from '@/components/PageState.vue'
import OrderProductEntry from '@/components/OrderProductEntry.vue'
import { useUserAuthStore } from '@/stores/user-auth'
import { useMessageCenterStore } from '@/stores/message-center'

const route = useRoute()
const router = useRouter()
const auth = useUserAuthStore()
const messageCenter = useMessageCenterStore()
const order = ref<OrderDetail | null>(null)
const loading = ref(true)
const error = ref('')
const message = ref('')
const busy = ref(false)
const hidden = ref<OrderHideResult | null>(null)

function token(): string {
  if (!auth.accessToken) throw new Error('missing user token')
  return auth.accessToken
}
function statusLabel(status: string): string {
  return ({ pending_payment: '等待付款', paid: '已支付', pending_shipment: '商家正在备货', shipped: '商品运输中', completed: '订单已完成', cancelled: '订单已取消', closed: '订单已关闭', unpaid: '未付款', unfulfilled: '未履约', none: '无售后' } as Record<string, string>)[status] ?? status
}
function eventLabel(code: string, target: string): string {
  if (code === 'order.created') return `订单已创建：${statusLabel(target)}`
  return `${code}：${statusLabel(target)}`
}
function actionLabel(code: string): string {
  return ({ pay: '去支付', cancel_order: '取消订单', apply_after_sale: '申请售后', view_after_sale: '查看售后', view_logistics: '查看物流', review: '评价', delete_order: '删除订单', confirm_receipt: '确认收货', contact_store: '联系商家', repurchase: '再次购买' } as Record<string, string>)[code] ?? code
}
function dateTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'long', timeStyle: 'short' }).format(new Date(value))
}
async function load() {
  loading.value = true
  error.value = ''
  try { order.value = (await getMyOrder(String(route.params.orderId), token())).data }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}
async function runAction(action: OrderAction) {
  if (!order.value || busy.value || !action.enabled) return
  if (['pay', 'apply_after_sale', 'view_after_sale', 'view_logistics', 'review'].includes(action.code)) {
    await router.push({ name: action.target.name, params: action.target.params })
    return
  }
  if (action.code === 'contact_store') {
    busy.value = true
    try {
      const conversation = (await ensureStoreConversation(order.value.store.store_id, token())).data
      await setConversationContext(conversation.conversation_id, conversation.version, 'order', order.value.order_id, order.value.version, token())
      messageCenter.show(conversation.conversation_id)
    } catch (cause) { error.value = errorMessage(cause) }
    finally { busy.value = false }
    return
  }
  if (['cancel_order', 'confirm_receipt', 'delete_order'].includes(action.code) && !window.confirm(`${actionLabel(action.code)}？服务端会再次校验订单状态。`)) return
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    if (action.code === 'cancel_order') {
      const description = window.prompt('可选：请补充取消原因（最多 200 字）')?.trim() || undefined
      await cancelOrder(order.value.order_id, order.value.version, token(), 'no_longer_needed', description)
      await load()
    } else if (action.code === 'confirm_receipt') {
      await confirmOrderReceipt(order.value.order_id, order.value.version, token())
      await load()
    } else if (action.code === 'delete_order') {
      hidden.value = (await hideOrder(order.value.order_id, order.value.version, token())).data
      message.value = `订单已隐藏，可在 ${dateTime(hidden.value.undo_until)} 前撤销。`
    } else if (action.code === 'repurchase') {
      const cart = await getCart(token())
      const result = (await repurchaseOrder(order.value.order_id, cart.data.version, token())).data
      if (result.requires_reselection) message.value = `${result.added_items.length} 件已加入购物车，${result.unavailable_items.length} 件原规格当前不可购买。`
      else await router.push('/cart')
    }
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}
async function undoHide() {
  if (!hidden.value || busy.value) return
  busy.value = true
  try {
    await restoreOrder(hidden.value, token())
    hidden.value = null
    message.value = '订单已恢复。'
    await load()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}
onMounted(load)
</script>

<template>
  <section class="order-detail-page">
    <RouterLink class="back-link" to="/me/orders">← 返回我的订单</RouterLink>
    <div v-if="error" class="alert error" role="alert">{{ error }}</div>
    <div v-if="message" class="alert success" role="status">{{ message }} <button v-if="hidden" type="button" class="link-button" :disabled="busy" @click="undoHide">撤销隐藏</button></div>
    <PageState :loading="loading" :error="''" :empty="!order" empty-title="未找到订单" empty-message="该订单不存在或已不可见。" @retry="load">
      <template v-if="order">
        <header class="order-detail-hero">
          <div><p class="eyebrow">订单状态</p><h1>{{ statusLabel(order.order_status) }}</h1><p>订单号 {{ order.order_id }} · {{ dateTime(order.created_at) }}</p></div>
          <div class="order-actions">
            <button v-for="action in order.available_actions" :key="action.code" type="button" :disabled="busy || !action.enabled || hidden !== null" @click="runAction(action)">{{ busy ? '处理中…' : actionLabel(action.code) }}</button>
          </div>
        </header>
        <div class="order-detail-grid">
          <main class="order-detail-main">
            <article class="card order-section">
              <header class="card-heading"><div><p class="eyebrow">商品清单</p><h2>{{ order.store.store_name }}</h2></div><RouterLink :to="`/stores/${order.store.store_id}`">进入店铺 →</RouterLink></header>
              <OrderProductEntry
                v-for="item in order.items"
                :key="item.order_item_id"
                class="order-detail-item"
                :product-id="item.product_id"
                :sku-id="item.sku_id"
                :product-name="item.product_name"
                :product-available="item.product_available"
              >
                <img v-if="item.image_url" :src="resolveApiAssetUrl(item.image_url) ?? ''" width="88" height="88" :alt="item.product_name" />
                <div v-else class="order-image-placeholder">商品</div>
                <span><strong>{{ item.product_name }}</strong><small>{{ item.sku_name }}</small><small>{{ item.spec_snapshot.map((spec) => `${spec.name}:${spec.value}`).join(' / ') }}</small><small v-if="!item.product_available" class="order-product-unavailable-badge">已下架</small></span>
                <span class="item-price">{{ formatMoney(item.unit_price) }}<small>× {{ item.quantity }}</small></span>
              </OrderProductEntry>
            </article>
            <article class="card order-section">
              <p class="eyebrow">订单进度</p><h2>状态时间线</h2>
              <ol class="timeline">
                <li v-for="event in order.events" :key="event.event_id"><strong>{{ eventLabel(event.event_code, event.to_status) }}</strong><time :datetime="event.occurred_at">{{ dateTime(event.occurred_at) }}</time><p v-if="event.reason">{{ event.reason }}</p></li>
              </ol>
            </article>
          </main>
          <aside class="order-detail-side">
            <article class="card order-section"><p class="eyebrow">收货信息</p><h2>{{ order.address.recipient_name }}</h2><p>{{ order.address.phone_masked }}</p><p>{{ order.address.province_code }} {{ order.address.city_code }} {{ order.address.district_code }} {{ order.address.address }}</p><small>此处展示下单时的地址快照，修改地址簿不会影响本订单。</small></article>
            <article class="card order-section"><p class="eyebrow">金额明细</p><dl class="amount-list"><dt>商品金额</dt><dd>{{ formatMoney(order.amounts.goods_amount) }}</dd><dt>运费</dt><dd>{{ formatMoney(order.amounts.freight_amount) }}</dd><dt>调整金额</dt><dd>{{ formatMoney(order.amounts.adjustment_amount) }}</dd><dt>实付金额</dt><dd>{{ formatMoney(order.amounts.paid_amount) }}</dd><dt class="total">应付金额</dt><dd class="total">{{ formatMoney(order.amounts.payable_amount) }}</dd></dl></article>
            <article v-if="order.buyer_remark" class="card order-section"><p class="eyebrow">买家留言</p><p>{{ order.buyer_remark }}</p></article>
            <article v-if="order.fulfillment_status === 'unfulfilled'" class="alert info"><strong>物流信息</strong><p>商家正在备货，暂无物流信息。</p></article>
          </aside>
        </div>
      </template>
    </PageState>
  </section>
</template>
