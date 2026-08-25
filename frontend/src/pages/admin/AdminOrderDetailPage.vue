<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { formatMoney } from '@/api/catalog'
import { errorMessage } from '@/api/http'
import { createAdminShipment } from '@/api/logistics'
import {
  adjustAdminOrderAmount,
  cancelAdminOrder,
  getAdminOrder,
  type AdminOrderDetail,
} from '@/api/orders'
import { useAdminAuthStore } from '@/stores/admin-auth'

const route = useRoute()
const router = useRouter()
const auth = useAdminAuthStore()
const item = ref<AdminOrderDetail | null>(null)
const etag = ref('')
const error = ref('')
const notice = ref('')
const busy = ref(false)
const adjustment = reactive({ minor_units: '0', reason_code: 'MANUAL_PRICE_ADJUSTMENT', reason: '' })
const cancellation = reactive({ reason_code: 'ADMIN_ORDER_CANCELLATION', reason: '' })
const shipment = reactive({ carrier_code: 'fake_express', carrier_name: '模拟快递', tracking_no: '', quantities: {} as Record<string, number> })

const order = computed(() => item.value?.order ?? null)
const canAdjust = computed(() => auth.has('orders:adjust') && item.value?.available_admin_actions.includes('adjust_amount'))
const canCancel = computed(() => auth.has('orders:cancel') && item.value?.available_admin_actions.includes('cancel'))
const canShip = computed(() => auth.has('shipments:create') && item.value?.available_admin_actions.includes('create_shipment'))

function dateTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}

function accept(response: { data: AdminOrderDetail; headers: Headers }) {
  item.value = response.data
  etag.value = response.headers.get('etag') ?? ''
  adjustment.minor_units = response.data.order.amounts.adjustment_amount.minor_units
  shipment.quantities = Object.fromEntries(response.data.order.items.map((line) => [line.order_item_id, line.quantity - line.refunded_quantity]))
}

async function load() {
  accept(await getAdminOrder(String(route.params.orderId), auth.accessToken!))
}

async function adjustAmount() {
  if (!item.value || !adjustment.reason.trim()) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    accept(await adjustAdminOrderAmount(item.value.order.order_id, etag.value, adjustment.minor_units, item.value.order.amounts.payable_amount.currency, adjustment.reason_code, adjustment.reason.trim(), auth.accessToken!))
    notice.value = '订单金额已调整，订单项分摊与合并交易单金额已同步更新。'
    adjustment.reason = ''
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}

async function cancel() {
  if (!item.value || !cancellation.reason.trim()) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    accept(await cancelAdminOrder(item.value.order.order_id, etag.value, cancellation.reason_code, cancellation.reason.trim(), auth.accessToken!))
    notice.value = '合并交易单已关闭，关联订单与库存预占已按事务处理。'
    cancellation.reason = ''
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}

async function createShipment() {
  if (!item.value || !shipment.tracking_no.trim()) return
  const lines = item.value.order.items
    .map((line) => ({ order_item_id: line.order_item_id, quantity: Number(shipment.quantities[line.order_item_id] ?? 0) }))
    .filter((line) => line.quantity > 0)
  if (!lines.length) { error.value = '至少选择一个发货商品并填写正整数数量。'; return }
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const response = await createAdminShipment(item.value.order.order_id, etag.value, { carrier_code: shipment.carrier_code, carrier_name: shipment.carrier_name, tracking_no: shipment.tracking_no.trim(), items: lines }, auth.accessToken!)
    await router.push(`/admin/shipments/${response.data.shipment_id}`)
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}

onMounted(() => load().catch((cause) => { error.value = errorMessage(cause) }))
</script>

<template>
  <section v-if="item && order" class="admin-page-stack">
    <header class="page-heading"><div><p class="eyebrow">订单 · {{ order.order_id }}</p><h1>{{ order.store.store_name }}</h1><p class="muted">交易 {{ order.trade_order_id }} · 用户 {{ item.user_name_masked }}（{{ item.user_id }}）</p></div></header>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p><p v-if="notice" class="alert success" role="status">{{ notice }}</p>
    <div class="settings-grid">
      <article class="card"><h2>正交状态</h2><dl class="detail-list"><dt>订单</dt><dd>{{ order.order_status }}</dd><dt>支付</dt><dd>{{ order.payment_status }}</dd><dt>履约</dt><dd>{{ order.fulfillment_status }}</dd><dt>售后</dt><dd>{{ order.after_sale_status }}</dd><dt>创建时间</dt><dd>{{ dateTime(order.created_at) }}</dd><dt>当前版本</dt><dd>v{{ order.version }}</dd></dl></article>
      <article class="card"><h2>金额事实</h2><dl class="detail-list"><dt>商品</dt><dd>{{ formatMoney(order.amounts.goods_amount) }}</dd><dt>运费</dt><dd>{{ formatMoney(order.amounts.freight_amount) }}</dd><dt>调整</dt><dd>{{ formatMoney(order.amounts.adjustment_amount) }}</dd><dt>应付</dt><dd>{{ formatMoney(order.amounts.payable_amount) }}</dd><dt>已付</dt><dd>{{ formatMoney(order.amounts.paid_amount) }}</dd><dt>已退</dt><dd>{{ formatMoney(order.amounts.refunded_amount) }}</dd></dl></article>
    </div>
    <article class="card"><h2>商品明细</h2><div class="table-wrap"><table><thead><tr><th>商品</th><th>数量</th><th>原价</th><th>应付</th><th>已退</th></tr></thead><tbody><tr v-for="line in order.items" :key="line.order_item_id"><td><strong>{{ line.product_name }}</strong><small>{{ line.sku_name }} · {{ line.order_item_id }}</small></td><td>{{ line.quantity }}</td><td>{{ formatMoney(line.gross_amount) }}</td><td>{{ formatMoney(line.payable_amount) }}</td><td>{{ formatMoney(line.refunded_amount) }}</td></tr></tbody></table></div></article>

    <div v-if="canAdjust || canCancel" class="settings-grid">
      <form v-if="canAdjust" class="card" @submit.prevent="adjustAmount"><h2>调整未支付订单金额</h2><p>填写目标调整总额（最小货币单位，人民币即“分”）；服务端重新分摊到订单项并维护交易单总额。</p><label>目标调整额（分，可为负数）<input v-model.trim="adjustment.minor_units" required pattern="-?\d+" inputmode="numeric" /></label><label>原因码<input v-model.trim="adjustment.reason_code" required pattern="[A-Z][A-Z0-9_]{1,63}" /></label><label>原因<textarea v-model.trim="adjustment.reason" required minlength="2" maxlength="500" /></label><button :disabled="busy || !adjustment.reason">确认调整</button></form>
      <form v-if="canCancel" class="card" @submit.prevent="cancel"><h2>取消待支付交易</h2><p>该订单属于合并交易单；操作会取消同一交易下全部店铺订单并释放库存。缺少任一关联店铺 Scope 时服务端拒绝。</p><label>原因码<input v-model.trim="cancellation.reason_code" required pattern="[A-Z][A-Z0-9_]{1,63}" /></label><label>取消原因<textarea v-model.trim="cancellation.reason" required minlength="2" maxlength="500" /></label><button class="danger" :disabled="busy || !cancellation.reason">确认取消整组交易</button></form>
    </div>

    <form v-if="canShip" class="card" @submit.prevent="createShipment"><h2>创建包裹并确认发货</h2><p>包裹没有可编辑草稿；成功提交即进入发货状态，运单号只在输入时使用，后续仅显示脱敏值。</p><div class="settings-grid"><label>承运商代码<input v-model.trim="shipment.carrier_code" required pattern="[a-z][a-z0-9_]{1,31}" /></label><label>承运商名称<input v-model.trim="shipment.carrier_name" required maxlength="64" /></label><label>运单号<input v-model.trim="shipment.tracking_no" required minlength="6" maxlength="64" autocomplete="off" /></label></div><fieldset><legend>本包裹商品数量</legend><label v-for="line in order.items" :key="line.order_item_id">{{ line.product_name }} · {{ line.sku_name }}<input v-model.number="shipment.quantities[line.order_item_id]" type="number" min="0" :max="line.quantity - line.refunded_quantity" /></label></fieldset><button :disabled="busy || !shipment.tracking_no">创建并发货</button></form>

    <article class="card"><h2>状态与操作流水</h2><ol class="timeline"><li v-for="event in item.events" :key="event.event_id"><strong>{{ event.event_code }}</strong><p>{{ event.state_dimension }}：{{ event.from_status ?? '创建' }} → {{ event.to_status }}</p><p v-if="event.reason">{{ event.reason }}</p><time>{{ dateTime(event.occurred_at) }}</time></li></ol></article>
  </section>
  <p v-else-if="!error">正在加载…</p><p v-else class="alert error" role="alert">{{ error }}</p>
</template>
