<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { formatMoney } from '@/api/catalog'
import { errorMessage, resolveApiAssetUrl } from '@/api/http'
import { listMyOrders, type OrderSummary, type OrderView } from '@/api/orders'
import PageState from '@/components/PageState.vue'
import { useUserAuthStore } from '@/stores/user-auth'

const views: Array<{ value: OrderView; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'pending_payment', label: '待付款' },
  { value: 'pending_shipment', label: '待发货' },
  { value: 'in_transit', label: '运输中' },
  { value: 'completed', label: '已完成' },
  { value: 'pending_review', label: '待评价' },
  { value: 'after_sale', label: '售后' },
  { value: 'cancelled', label: '已取消' },
]
const route = useRoute()
const router = useRouter()
const auth = useUserAuthStore()
const items = ref<OrderSummary[]>([])
const loading = ref(true)
const error = ref('')
const searchText = ref(String(route.query.q ?? ''))
const previousCursor = ref<string | null>(null)
const nextCursor = ref<string | null>(null)
const view = computed<OrderView>(() => {
  const candidate = String(route.query.view ?? 'all') as OrderView
  return views.some((item) => item.value === candidate) ? candidate : 'all'
})

function token(): string {
  if (!auth.accessToken) throw new Error('missing user token')
  return auth.accessToken
}
function statusLabel(status: string): string {
  return ({ pending_payment: '待付款', paid: '已支付', pending_shipment: '待发货', shipped: '运输中', completed: '已完成', cancelled: '已取消', closed: '已关闭' } as Record<string, string>)[status] ?? status
}
function actionLabel(code: string): string {
  return ({ pay: '去支付', cancel_order: '取消订单', apply_after_sale: '申请售后', view_after_sale: '查看售后', view_logistics: '查看物流', review: '评价', delete_order: '删除订单', confirm_receipt: '确认收货', contact_store: '联系商家', repurchase: '再次购买' } as Record<string, string>)[code] ?? code
}
function dateTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value))
}
async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await listMyOrders({
      view: view.value,
      q: typeof route.query.q === 'string' ? route.query.q : undefined,
      cursor: typeof route.query.cursor === 'string' ? route.query.cursor : undefined,
      limit: 10,
    }, token())
    items.value = response.data.items
    previousCursor.value = response.meta.pagination?.previous_cursor ?? null
    nextCursor.value = response.meta.pagination?.next_cursor ?? null
  } catch (cause) {
    error.value = errorMessage(cause)
    items.value = []
  } finally {
    loading.value = false
  }
}
function chooseView(nextView: OrderView) {
  void router.push({ path: '/me/orders', query: { view: nextView, ...(searchText.value.trim() ? { q: searchText.value.trim() } : {}) } })
}
function submitSearch() {
  void router.push({ path: '/me/orders', query: { view: view.value, ...(searchText.value.trim() ? { q: searchText.value.trim() } : {}) } })
}
function page(cursor: string | null) {
  if (!cursor) return
  void router.push({ path: '/me/orders', query: { view: view.value, ...(route.query.q ? { q: route.query.q } : {}), cursor } })
}
watch(() => route.fullPath, () => {
  searchText.value = String(route.query.q ?? '')
  void load()
}, { immediate: true })
</script>

<template>
  <section class="order-page">
    <header class="page-heading">
      <div><p class="eyebrow">订单中心</p><h1>我的订单</h1><p class="muted">订单状态与可执行操作均以服务端实时结果为准。</p></div>
      <RouterLink to="/me">返回我的</RouterLink>
    </header>
    <nav class="order-tabs" aria-label="订单视图">
      <button v-for="item in views" :key="item.value" type="button" :class="{ active: view === item.value }" @click="chooseView(item.value)">{{ item.label }}</button>
    </nav>
    <form class="order-search" role="search" @submit.prevent="submitSearch">
      <label><span class="sr-only">搜索订单</span><input v-model="searchText" maxlength="64" placeholder="搜索订单号、商品名称或店铺名称" /></label>
      <button type="submit">搜索</button>
    </form>
    <div v-if="error" class="alert error" role="alert">{{ error }}</div>
    <PageState :loading="loading" :error="''" :empty="items.length === 0" empty-title="暂时没有相关订单" empty-message="可以切换分类或修改搜索词后重试。" @retry="load">
      <div class="order-list">
        <article v-for="order in items" :key="order.order_id" class="order-card">
          <header>
            <div><time :datetime="order.created_at">{{ dateTime(order.created_at) }}</time><small>订单号 {{ order.order_id }}</small></div>
            <strong class="order-status">{{ statusLabel(order.order_status) }}</strong>
          </header>
          <div class="order-store-row">
            <RouterLink class="store-identity" :to="`/stores/${order.store.store_id}`">
              <img v-if="order.store.logo_url" :src="resolveApiAssetUrl(order.store.logo_url) ?? ''" width="36" height="36" alt="" />
              <span v-else class="mini-store-logo">{{ order.store.store_name.slice(0, 1) }}</span>
              <strong>{{ order.store.store_name }}</strong><span aria-hidden="true">→</span>
            </RouterLink>
          </div>
          <RouterLink class="order-card-main" :to="`/me/orders/${order.order_id}`">
            <div v-for="item in order.items.slice(0, 3)" :key="item.order_item_id" class="order-item-summary">
              <img v-if="item.image_url" :src="resolveApiAssetUrl(item.image_url) ?? ''" width="72" height="72" :alt="item.product_name" />
              <div v-else class="order-image-placeholder">商品</div>
              <span><strong>{{ item.product_name }}</strong><small>{{ item.sku_name }} · × {{ item.quantity }}</small></span>
              <strong>{{ formatMoney(item.payable_amount) }}</strong>
            </div>
            <small v-if="order.item_count > 3">另有 {{ order.item_count - 3 }} 件商品，点击查看全部</small>
          </RouterLink>
          <footer>
            <span>共 {{ order.total_quantity }} 件，应付 <strong>{{ formatMoney(order.amounts.payable_amount) }}</strong></span>
            <div class="order-actions">
              <RouterLink v-for="action in order.available_actions" :key="action.code" :class="['order-action', { disabled: !action.enabled }]" :to="action.enabled ? { name: action.target.name, params: action.target.params } : ''" :aria-disabled="!action.enabled" :title="action.reason_message ?? undefined">{{ actionLabel(action.code) }}</RouterLink>
            </div>
          </footer>
        </article>
      </div>
      <nav v-if="previousCursor || nextCursor" class="pagination" aria-label="订单分页">
        <button type="button" class="secondary" :disabled="!previousCursor" @click="page(previousCursor)">上一页</button>
        <button type="button" class="secondary" :disabled="!nextCursor" @click="page(nextCursor)">下一页</button>
      </nav>
    </PageState>
  </section>
</template>
