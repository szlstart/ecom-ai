<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'

import { adminGet, requireAdminToken, type AdminStore } from '@/api/admin-catalog'
import { listAdminRefunds } from '@/api/admin-after-sales'
import { formatMoney, type Money } from '@/api/catalog'
import { apiRequest, errorMessage, resolveApiAssetUrl } from '@/api/http'
import { createAdminShipment } from '@/api/logistics'
import { getAdminOrder, listAdminOrders, type AdminOrderDetail, type AdminOrderSummary } from '@/api/orders'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

interface RevenueDashboard {
  gross_sales: Money; refunded_amount: Money; net_revenue: Money
  today_revenue: Money; yesterday_revenue: Money; last_30_days_revenue: Money
  all_order_count: number; completed_order_count: number; pending_payment_count: number
  pending_shipment_count: number; in_transit_count: number; after_sale_pending_count: number
  cancelled_count: number
}
type OrderView = 'all' | 'pending_payment' | 'pending_shipment' | 'in_transit' | 'completed' | 'after_sale' | 'cancelled'

const auth = useAdminAuthStore()
const store = ref<AdminStore | null>(null)
const revenue = ref<RevenueDashboard | null>(null)
const items = ref<AdminOrderSummary[]>([])
const nextCursor = ref<string | null>(null)
const refundByOrderId = ref<Record<string, string>>({})
const loadingMore = ref(false)
const view = ref<OrderView>('all')
const loading = ref(true)
const error = ref('')
const notice = ref('')
const shippingOrder = ref<AdminOrderDetail | null>(null)
const shipmentEtag = ref('')
const shipmentBusy = ref(false)
const shipment = reactive({ carrier_code: 'fake_express', carrier_name: '模拟快递', tracking_no: '', quantities: {} as Record<string, number> })
let refreshTimer: number | undefined

const tabs = computed(() => [
  { value: 'all' as const, label: '全部订单', count: revenue.value?.all_order_count ?? 0 },
  { value: 'pending_payment' as const, label: '待付款', count: revenue.value?.pending_payment_count ?? 0 },
  { value: 'pending_shipment' as const, label: '待发货', count: revenue.value?.pending_shipment_count ?? 0 },
  { value: 'in_transit' as const, label: '运输中', count: revenue.value?.in_transit_count ?? 0 },
  { value: 'completed' as const, label: '已完成订单', count: revenue.value?.completed_order_count ?? 0 },
  { value: 'after_sale' as const, label: '售后待处理', count: revenue.value?.after_sale_pending_count ?? 0 },
  { value: 'cancelled' as const, label: '已取消', count: revenue.value?.cancelled_count ?? 0 },
])

function token() { return requireAdminToken(auth.accessToken) }
function filters(): Record<string, string> {
  if (view.value === 'after_sale') return { after_sale_status: 'in_progress' }
  if (view.value === 'in_transit') return { order_status: 'shipped' }
  if (view.value === 'all') return {}
  return { order_status: view.value }
}
function statusLabel(order: AdminOrderSummary['order']) {
  if (order.after_sale_status === 'in_progress') return '售后处理中'
  return ({ pending_payment: '等待顾客付款', pending_shipment: '等待发货', shipped: '运输中', completed: '已完成', cancelled: '已取消', closed: '已关闭' } as Record<string, string>)[order.order_status] ?? order.order_status
}
function dateTime(value: string) { return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) }

async function load(silent = false) {
  if (!silent) loading.value = true
  error.value = ''
  try {
    if (!store.value) {
      store.value = (await adminGet<{ items: AdminStore[] }>('/admin/stores?limit=20', token())).data.items[0] ?? null
    }
    if (!store.value) throw new Error('当前账号没有绑定店铺。')
    const [revenueResult, orderResult, refundResult] = await Promise.all([
      apiRequest<RevenueDashboard>(`/merchant/stores/${encodeURIComponent(store.value.store_id)}/revenue`, {}, token()),
      listAdminOrders(filters(), token()),
      listAdminRefunds(token()),
    ])
    revenue.value = revenueResult.data
    items.value = orderResult.data.items
    nextCursor.value = orderResult.data.next_cursor
    refundByOrderId.value = Object.fromEntries(refundResult.data.items.map((item) => [item.order_id, item.refund_id]))
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}
async function chooseView(next: OrderView) { view.value = next; await load() }
async function loadMore() {
  if (!nextCursor.value || loadingMore.value) return
  loadingMore.value = true; error.value = ''
  try {
    const response = await listAdminOrders(filters(), token(), nextCursor.value)
    const existing = new Set(items.value.map((item) => item.order.order_id))
    items.value.push(...response.data.items.filter((item) => !existing.has(item.order.order_id)))
    nextCursor.value = response.data.next_cursor
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loadingMore.value = false }
}

async function beginShipment(item: AdminOrderSummary) {
  error.value = ''; notice.value = ''
  try {
    const response = await getAdminOrder(item.order.order_id, token())
    shippingOrder.value = response.data
    shipmentEtag.value = response.headers.get('etag') ?? ''
    shipment.tracking_no = ''
    shipment.quantities = { ...response.data.shippable_quantities }
  } catch (cause) { error.value = errorMessage(cause) }
}
function closeShipment() { if (!shipmentBusy.value) shippingOrder.value = null }
async function submitShipment() {
  if (!shippingOrder.value || !shipment.tracking_no.trim()) return
  const lines = shippingOrder.value.order.items.map((line) => ({ order_item_id: line.order_item_id, quantity: Number(shipment.quantities[line.order_item_id] ?? 0) })).filter((line) => line.quantity > 0)
  if (!lines.length) { error.value = '至少选择一件需要发货的商品。'; return }
  shipmentBusy.value = true; error.value = ''
  try {
    await createAdminShipment(shippingOrder.value.order.order_id, shipmentEtag.value, { carrier_code: shipment.carrier_code, carrier_name: shipment.carrier_name, tracking_no: shipment.tracking_no.trim(), items: lines }, token())
    shippingOrder.value = null
    notice.value = '包裹已创建，订单和顾客端物流状态已经同步更新。'
    await load(true)
  } catch (cause) { error.value = errorMessage(cause) }
  finally { shipmentBusy.value = false }
}

onMounted(() => {
  void load()
  refreshTimer = window.setInterval(() => { if (!shippingOrder.value && document.visibilityState === 'visible') void load(true) }, 15_000)
})
onUnmounted(() => { if (refreshTimer) window.clearInterval(refreshTimer) })
</script>

<template>
  <section class="merchant-page-stack merchant-orders-page">
    <header class="merchant-page-heading"><div><p class="eyebrow">订单与收益</p><h1>我的订单</h1><p>顾客确认收货后才计入营业额；物流签收满 7 天仍未操作时，系统自动确认收货。</p></div><button type="button" class="secondary" :disabled="loading" @click="load()">刷新订单</button></header>
    <p v-if="notice" class="alert success" role="status">{{ notice }}</p><p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <section v-if="revenue" class="merchant-income-grid" aria-label="店铺收益概览"><article class="primary"><span>总营业额</span><strong>{{ formatMoney(revenue.net_revenue) }}</strong><small>累计确认收货金额减累计退款</small></article><article><span>今日收益</span><strong>{{ formatMoney(revenue.today_revenue) }}</strong></article><article><span>昨日收益</span><strong>{{ formatMoney(revenue.yesterday_revenue) }}</strong></article><article><span>近 30 日收益</span><strong>{{ formatMoney(revenue.last_30_days_revenue) }}</strong></article></section>
    <nav class="merchant-order-tabs" aria-label="商家订单分类"><button v-for="tab in tabs" :key="tab.value" type="button" :class="{ active: view === tab.value, urgent: tab.value === 'after_sale' && tab.count > 0 }" :aria-pressed="view === tab.value" @click="chooseView(tab.value)"><span>{{ tab.label }}</span><b>{{ tab.count }}</b></button></nav>
    <PageState :loading="loading" :show-refresh-status="false" :error="''" :empty="!items.length" empty-title="当前分类没有订单" empty-message="新订单或售后申请出现后，这里会自动刷新。" @retry="load()">
      <div class="merchant-order-list"><article v-for="item in items" :key="item.order.order_id" class="merchant-order-card" :class="{ aftersale: item.order.after_sale_status === 'in_progress' }"><header><div><strong>{{ statusLabel(item.order) }}</strong><small>{{ dateTime(item.order.created_at) }} · 订单 {{ item.order.order_id }}</small></div><span>顾客 {{ item.user_name_masked }}</span></header><div class="merchant-order-lines"><div v-for="line in item.order.items" :key="line.order_item_id"><img v-if="line.image_url" :src="resolveApiAssetUrl(line.image_url) ?? ''" width="64" height="64" alt="" /><div v-else class="order-image-placeholder">商品</div><span><strong>{{ line.product_name }}</strong><small>{{ line.sku_name }} · × {{ line.quantity }}</small><small v-if="line.after_sale_status !== 'none'" class="danger-text">该商品正在售后处理中</small></span><b>{{ formatMoney(line.payable_amount) }}</b></div></div><footer><span>实付 {{ formatMoney(item.order.amounts.paid_amount) }}<small v-if="item.order.amounts.refunded_amount.minor_units !== '0'">已退款 {{ formatMoney(item.order.amounts.refunded_amount) }}</small></span><div class="actions"><button v-if="item.available_admin_actions.includes('create_shipment')" type="button" @click="beginShipment(item)">去发货</button><RouterLink v-if="item.order.after_sale_status === 'in_progress' && refundByOrderId[item.order.order_id]" class="merchant-after-sale-chip" :to="`/merchant/after-sales/${refundByOrderId[item.order.order_id]}`">处理售后</RouterLink><RouterLink v-else-if="item.order.after_sale_status === 'in_progress'" class="merchant-after-sale-chip" to="/merchant/after-sales">查看售后</RouterLink></div></footer></article><button v-if="nextCursor" type="button" class="secondary merchant-load-more" :disabled="loadingMore" @click="loadMore">{{ loadingMore ? '正在加载…' : '加载更多订单' }}</button></div>
    </PageState>
    <Teleport to="body"><div v-if="shippingOrder" class="merchant-delete-overlay" @mousedown.self="closeShipment"><form class="merchant-shipment-dialog" @submit.prevent="submitShipment"><header><div><p class="eyebrow">订单 {{ shippingOrder.order.order_id }}</p><h2>创建包裹并发货</h2></div><button type="button" class="secondary small" @click="closeShipment">关闭</button></header><div class="field-grid"><label>快递公司<select v-model="shipment.carrier_code" @change="shipment.carrier_name = shipment.carrier_code === 'fake_express' ? '模拟快递' : '其他快递'"><option value="fake_express">模拟快递</option><option value="other_express">其他快递</option></select></label><label>运单号<input v-model.trim="shipment.tracking_no" required minlength="6" maxlength="64" placeholder="请输入快递运单号" /></label></div><fieldset><legend>本次发货数量</legend><label v-for="line in shippingOrder.order.items.filter((item) => (shippingOrder!.shippable_quantities[item.order_item_id] ?? 0) > 0)" :key="line.order_item_id">{{ line.product_name }} · {{ line.sku_name }}（最多 {{ shippingOrder.shippable_quantities[line.order_item_id] ?? 0 }} 件）<input v-model.number="shipment.quantities[line.order_item_id]" type="number" min="0" :max="shippingOrder.shippable_quantities[line.order_item_id] ?? 0" /></label></fieldset><p>提交后顾客端会进入运输中状态；物流签收后顾客可确认收货，满 7 天未操作则自动确认。</p><button :disabled="shipmentBusy || !shipment.tracking_no">{{ shipmentBusy ? '正在创建包裹…' : '确认发货' }}</button></form></div></Teleport>
  </section>
</template>
