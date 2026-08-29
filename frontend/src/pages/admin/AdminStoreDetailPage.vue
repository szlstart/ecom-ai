<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { adminCommand, adminDelete, adminGet, adminQuery, adminUpdate, requireAdminToken, type AdminProduct, type AdminProductDeletionEligibility, type AdminProductSummary, type AdminStore } from '@/api/admin-catalog'
import { formatMoney, type Money } from '@/api/catalog'
import { ApiProblem, apiRequest, errorMessage, resolveApiAssetUrl } from '@/api/http'
import { listAdminOrders, type AdminOrderSummary } from '@/api/orders'
import AdminFileUpload from '@/components/AdminFileUpload.vue'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'
import { publishStoreStatus } from '@/utils/store-status-sync'

type WorkspaceSection = 'products' | 'orders'
type ProductStatus = '' | 'on_sale' | 'draft' | 'pending_review' | 'rejected' | 'off_shelf'
type OrderView = 'all' | 'pending_payment' | 'pending_shipment' | 'in_transit' | 'completed' | 'after_sale'
interface RevenueDashboard {
  gross_sales: Money; refunded_amount: Money; net_revenue: Money
  today_revenue: Money; yesterday_revenue: Money; last_30_days_revenue: Money
  all_order_count: number; completed_order_count: number; pending_payment_count: number
  pending_shipment_count: number; in_transit_count: number; after_sale_pending_count: number
  product_count: number
}

const route = useRoute(); const router = useRouter(); const auth = useAdminAuthStore()
const storeId = computed(() => String(route.params.storeId))
const store = ref<AdminStore | null>(null)
const revenue = ref<RevenueDashboard | null>(null)
const products = ref<AdminProductSummary[]>([])
const orders = ref<AdminOrderSummary[]>([])
const orderNextCursor = ref<string | null>(null)
const orderLoadingMore = ref(false)
const section = ref<WorkspaceSection>(route.query.tab === 'orders' ? 'orders' : 'products')
const productStatus = ref<ProductStatus>('')
const orderView = ref<OrderView>('all')
const productQuery = ref('')
const loading = ref(true); const panelLoading = ref(false); const saving = ref(false)
const error = ref(''); const notice = ref('')
const editingProfile = ref(false)
const statusConfirmOpen = ref(false)
const deleteOpen = ref(false); const deleteReason = ref(''); const deleteConfirmation = ref('')
const deletingProduct = ref<AdminProductSummary | null>(null)
const productDeletionEligibility = ref<AdminProductDeletionEligibility | null>(null)
const checkingProductDeletion = ref(false)
const productDeletionBusy = ref(false)
const profile = reactive({ store_name: '', description: '', logo_file_id: '' })

const productTabs: Array<{ value: ProductStatus; label: string }> = [
  { value: '', label: '全部商品' }, { value: 'on_sale', label: '销售中' }, { value: 'draft', label: '草稿' },
  { value: 'pending_review', label: '审核中' }, { value: 'rejected', label: '需修改' }, { value: 'off_shelf', label: '已下架' },
]
const orderTabs = computed(() => [
  { value: 'all' as const, label: '全部订单', count: revenue.value?.all_order_count ?? 0 },
  { value: 'pending_payment' as const, label: '待付款', count: revenue.value?.pending_payment_count ?? 0 },
  { value: 'pending_shipment' as const, label: '待发货', count: revenue.value?.pending_shipment_count ?? 0 },
  { value: 'in_transit' as const, label: '运输中', count: revenue.value?.in_transit_count ?? 0 },
  { value: 'completed' as const, label: '已完成', count: revenue.value?.completed_order_count ?? 0 },
  { value: 'after_sale' as const, label: '售后待处理', count: revenue.value?.after_sale_pending_count ?? 0 },
])
const totalStock = computed(() => products.value.reduce((sum, item) => sum + item.available_quantity, 0))

function token() { return requireAdminToken(auth.accessToken) }
function endpoint(suffix = '') { return `/admin/stores/${encodeURIComponent(storeId.value)}${suffix}` }
function statusLabel(value: string) { return ({ active: '营业中', suspended: '已暂停', draft: '草稿', pending_review: '审核中', rejected: '需修改', on_sale: '销售中', off_shelf: '已下架' } as Record<string, string>)[value] ?? value }
function orderStatusLabel(item: AdminOrderSummary) {
  if (item.order.after_sale_status === 'in_progress') return '售后处理中'
  return ({ pending_payment: '等待顾客付款', pending_shipment: '等待店铺发货', shipped: '运输中', completed: '已完成', cancelled: '已取消', closed: '已关闭' } as Record<string, string>)[item.order.order_status] ?? item.order.order_status
}
function dateTime(value: string) { return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) }
function orderFilters(): Record<string, string> {
  return { store_id: storeId.value, view: orderView.value }
}

async function load() {
  loading.value = true; error.value = ''
  try {
    const [storeResult, revenueResult] = await Promise.all([
      adminGet<AdminStore>(endpoint(), token()),
      apiRequest<RevenueDashboard>(endpoint('/revenue'), {}, token()),
    ])
    store.value = storeResult.data; revenue.value = revenueResult.data
    Object.assign(profile, { store_name: store.value.store_name, description: store.value.description ?? '', logo_file_id: '' })
    if (section.value === 'orders') await loadOrders()
    else await loadProducts()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function loadProducts() {
  panelLoading.value = true; error.value = ''
  try {
    const result = await adminGet<{ items: AdminProductSummary[] }>(`/admin/products${adminQuery({ store_id: storeId.value, status: productStatus.value, q: productQuery.value.trim(), limit: 100 })}`, token())
    products.value = result.data.items
  } catch (cause) { error.value = errorMessage(cause) }
  finally { panelLoading.value = false }
}

async function loadOrders() {
  panelLoading.value = true; error.value = ''
  try {
    const response = await listAdminOrders(orderFilters(), token())
    orders.value = response.data.items
    orderNextCursor.value = response.data.next_cursor
  }
  catch (cause) { error.value = errorMessage(cause) }
  finally { panelLoading.value = false }
}

async function loadMoreOrders() {
  if (!orderNextCursor.value || orderLoadingMore.value) return
  orderLoadingMore.value = true; error.value = ''
  try {
    const response = await listAdminOrders(orderFilters(), token(), orderNextCursor.value)
    const existing = new Set(orders.value.map((item) => item.order.order_id))
    orders.value.push(...response.data.items.filter((item) => !existing.has(item.order.order_id)))
    orderNextCursor.value = response.data.next_cursor
  } catch (cause) { error.value = errorMessage(cause) }
  finally { orderLoadingMore.value = false }
}

async function chooseSection(value: WorkspaceSection) {
  section.value = value
  await router.replace({ query: { ...route.query, tab: value === 'orders' ? 'orders' : undefined } })
  if (value === 'orders') await loadOrders()
  else await loadProducts()
}
async function chooseProductStatus(value: ProductStatus) { productStatus.value = value; await loadProducts() }
async function chooseOrderView(value: OrderView) { orderView.value = value; await loadOrders() }

async function saveProfile() {
  if (!store.value) return
  saving.value = true; error.value = ''; notice.value = ''
  try {
    const payload: Record<string, unknown> = { store_name: profile.store_name, description: profile.description || null }
    if (profile.logo_file_id) payload.logo_file_id = profile.logo_file_id
    store.value = (await adminUpdate<AdminStore>(endpoint(), payload, token(), store.value.version)).data
    editingProfile.value = false; profile.logo_file_id = ''; notice.value = '店铺公开资料已更新，顾客端会同步显示最新内容。'
  } catch (cause) { error.value = errorMessage(cause) }
  finally { saving.value = false }
}

async function toggleStoreStatus() {
  if (!store.value) return
  statusConfirmOpen.value = false
  saving.value = true; error.value = ''; notice.value = ''
  const action = store.value.status === 'active' ? 'suspend' : 'resume'
  try {
    store.value = (await adminCommand<AdminStore>(endpoint('/status-changes'), { action, confirmed: true, reason_code: 'PLATFORM_OPERATIONS', reason: '超级管理员在店铺运营工作台确认调整经营状态。' }, token(), store.value.version, `admin-store-${action}`)).data
    publishStoreStatus({ storeId: store.value.store_id, status: store.value.status, suspensionSource: store.value.suspension_source })
    notice.value = action === 'suspend' ? '店铺已暂停营业，顾客端不再开放新的购买。' : '店铺已恢复营业。'
  } catch (cause) { error.value = errorMessage(cause) }
  finally { saving.value = false }
}

function closeProductDeletion() {
  if (productDeletionBusy.value) return
  deletingProduct.value = null
  productDeletionEligibility.value = null
}

async function beginProductDelete(item: AdminProductSummary) {
  deletingProduct.value = item
  productDeletionEligibility.value = null
  checkingProductDeletion.value = true
  error.value = ''; notice.value = ''
  try {
    productDeletionEligibility.value = (await adminGet<AdminProductDeletionEligibility>(
      `/admin/products/${encodeURIComponent(item.product_id)}/deletion-eligibility`,
      token(),
    )).data
  } catch (cause) {
    error.value = errorMessage(cause)
    deletingProduct.value = null
  } finally {
    checkingProductDeletion.value = false
  }
}

async function confirmProductDelete() {
  if (!deletingProduct.value || !productDeletionEligibility.value?.can_delete) return
  productDeletionBusy.value = true; error.value = ''
  try {
    await adminDelete(
      `/admin/products/${encodeURIComponent(deletingProduct.value.product_id)}`,
      token(),
      deletingProduct.value.version,
      'admin-store-product-delete',
    )
    const name = deletingProduct.value.product_name
    deletingProduct.value = null; productDeletionEligibility.value = null
    notice.value = `“${name}”没有产生过交易，已永久删除。`
    await loadProducts()
  } catch (cause) {
    if (cause instanceof ApiProblem && cause.body.code === 'PRODUCT_HAS_TRANSACTIONS' && deletingProduct.value) {
      productDeletionEligibility.value = {
        product_id: deletingProduct.value.product_id,
        current_status: deletingProduct.value.status,
        has_transactions: true,
        can_delete: false,
        can_off_shelf: deletingProduct.value.status === 'on_sale',
        recommended_action: deletingProduct.value.status === 'on_sale' ? 'off_shelf' : 'none',
        message: cause.body.detail,
      }
    } else {
      error.value = errorMessage(cause)
      deletingProduct.value = null; productDeletionEligibility.value = null
    }
  } finally {
    productDeletionBusy.value = false
  }
}

async function confirmProductOffShelf() {
  if (!deletingProduct.value || !productDeletionEligibility.value?.can_off_shelf) return
  productDeletionBusy.value = true; error.value = ''
  try {
    await adminCommand(
      `/admin/products/${encodeURIComponent(deletingProduct.value.product_id)}/off-shelf-commands`,
      { reason_code: 'HAS_TRANSACTION_DELETE_GUARD', reason: '商品已有交易记录，超级管理员在删除提示中选择下架。' },
      token(),
      deletingProduct.value.version,
      'admin-store-product-off-shelf',
    )
    const name = deletingProduct.value.product_name
    deletingProduct.value = null; productDeletionEligibility.value = null
    notice.value = `“${name}”已有交易记录，不能删除，现已下架。`
    await loadProducts()
  } catch (cause) {
    error.value = errorMessage(cause)
    deletingProduct.value = null; productDeletionEligibility.value = null
  } finally {
    productDeletionBusy.value = false
  }
}

async function toggleProduct(item: AdminProductSummary) {
  saving.value = true; error.value = ''; notice.value = ''
  const base = `/admin/products/${encodeURIComponent(item.product_id)}`
  const payload = { reason_code: 'PLATFORM_OPERATIONS', reason: '超级管理员在店铺运营工作台调整商品营业状态。' }
  try {
    if (item.status === 'on_sale') {
      await adminCommand<AdminProduct>(`${base}/off-shelf-commands`, payload, token(), item.version, 'admin-product-off-shelf')
      notice.value = `“${item.product_name}”已下架。`
    } else if (item.status === 'off_shelf') {
      const submitted = (await adminCommand<AdminProduct>(`${base}/review-submissions`, payload, token(), item.version, 'admin-product-resubmit')).data
      const approved = (await adminCommand<AdminProduct>(`${base}/moderation-decisions`, { ...payload, decision: 'approve' }, token(), submitted.version, 'admin-product-approve')).data
      await adminCommand<AdminProduct>(`${base}/publications`, payload, token(), approved.version, 'admin-product-republish')
      notice.value = `“${item.product_name}”已完成平台复核并重新上架。`
    }
    await loadProducts()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { saving.value = false }
}

async function deleteStore() {
  if (!store.value || deleteConfirmation.value !== 'DELETE_STORE' || deleteReason.value.trim().length < 2) return
  saving.value = true; error.value = ''
  try {
    await apiRequest(endpoint(), { method: 'DELETE', headers: { 'If-Match': `"v${store.value.version}"` }, body: JSON.stringify({ reason: deleteReason.value.trim(), confirmation: deleteConfirmation.value }) }, token())
    await router.replace({ path: '/admin/stores', query: { deleted: storeId.value } })
  } catch (cause) { error.value = errorMessage(cause); deleteOpen.value = false }
  finally { saving.value = false }
}

onMounted(load)
</script>

<template>
  <section class="admin-page-stack admin-store-workspace">
    <div class="admin-workspace-breadcrumb"><RouterLink to="/admin/stores">店铺运营</RouterLink><span>›</span><strong>{{ store?.store_name || storeId }}</strong></div>
    <p v-if="notice" class="alert success" aria-live="polite">{{ notice }}</p>
    <PageState :loading="loading" :error="error" :empty="!loading && !store" empty-title="店铺不存在" @retry="load">
      <template v-if="store">
        <header class="admin-store-workspace-hero">
          <div class="admin-store-workspace-brand"><span><img v-if="store.logo_url" :src="resolveApiAssetUrl(store.logo_url) || undefined" alt="" /><template v-else>{{ store.store_name.slice(0, 1) }}</template></span><div><p class="eyebrow">平台监管视角 · {{ store.store_id }}</p><h1>{{ store.store_name }}</h1><p>{{ store.description || '暂无店铺简介，管理员可以补充。' }}</p><small :class="store.status">● {{ statusLabel(store.status) }}</small></div></div>
          <div class="actions"><RouterLink class="button-link secondary" :to="`/stores/${storeId}`" target="_blank">查看顾客端 ↗</RouterLink><button v-if="auth.has('stores:manage')" type="button" class="secondary" @click="editingProfile = !editingProfile">编辑店铺资料</button><button v-if="auth.has('stores:manage')" type="button" :class="store.status === 'active' ? 'danger' : ''" :disabled="saving" @click="statusConfirmOpen = true">{{ store.status === 'active' ? '暂停营业' : '恢复营业' }}</button></div>
        </header>

        <form v-if="editingProfile" class="admin-store-profile-editor" @submit.prevent="saveProfile">
          <header><div><h2>编辑顾客看到的店铺资料</h2><p>本次修改由超级管理员执行并写入操作审计。</p></div><button type="button" class="secondary small" @click="editingProfile = false">取消</button></header>
          <div class="admin-store-profile-fields"><label>店铺名称<input v-model.trim="profile.store_name" required minlength="2" maxlength="128" /></label><label class="wide">店铺简介<textarea v-model.trim="profile.description" maxlength="2000" rows="4" /></label><AdminFileUpload purpose="store_logo" :business-context-id="storeId" label="上传新的店铺 Logo" @uploaded="profile.logo_file_id = $event" /></div>
          <footer><button type="button" class="danger" @click="deleteOpen = true">删除店铺</button><button :disabled="saving">{{ saving ? '正在保存…' : '保存店铺资料' }}</button></footer>
        </form>

        <section class="admin-store-metrics" aria-label="店铺经营概览">
          <article class="revenue"><span>总营业额</span><strong>{{ revenue ? formatMoney(revenue.net_revenue) : '—' }}</strong><small>顾客确认收货后计入，已扣除退款</small></article>
          <article><span>商品数量</span><strong>{{ revenue?.product_count ?? 0 }}</strong><small>当前店铺全部商品</small></article>
          <article><span>累计销量</span><strong>{{ store.sales_count }}</strong><small>历史成交件数</small></article>
          <article><span>店铺评分</span><strong>{{ Number(store.rating_score).toFixed(1) }}</strong><small>{{ store.rating_count }} 条有效评价</small></article>
        </section>

        <nav class="admin-store-workspace-tabs" aria-label="店铺监管内容"><button type="button" :class="{ active: section === 'products' }" @click="chooseSection('products')"><span>店铺的商品</span><small>查看、编辑与调整营业状态</small></button><button type="button" :class="{ active: section === 'orders' }" @click="chooseSection('orders')"><span>店铺的订单</span><small>监管履约、售后与异常交易</small></button></nav>

        <section v-if="section === 'products'" class="admin-store-panel">
          <header><div><p class="eyebrow">STORE PRODUCTS</p><h2>该店铺的商品</h2><p>点击商品后直接进入与商家端一致的所见即所得编辑页；操作身份仍是超级管理员并完整留痕。</p></div><RouterLink v-if="auth.has('products:create')" class="button-link" :to="{ path: `/admin/stores/${storeId}/products/new`, query: { return_to: route.fullPath } }">＋ 为该店铺新增商品</RouterLink></header>
          <div class="merchant-products-toolbar"><div class="merchant-segmented" aria-label="商品营业状态"><button v-for="tab in productTabs" :key="tab.value || 'all'" type="button" :class="{ active: productStatus === tab.value }" @click="chooseProductStatus(tab.value)">{{ tab.label }}</button></div><form class="merchant-product-search" @submit.prevent="loadProducts"><input v-model="productQuery" placeholder="搜索该店商品" /><button>搜索</button></form></div>
          <PageState :loading="panelLoading" :error="''" :empty="!products.length" empty-title="当前分类没有商品" @retry="loadProducts"><div class="admin-store-product-grid"><article v-for="item in products" :key="item.product_id" class="merchant-product-card merchant-manage-card"><RouterLink class="merchant-product-card-link" :to="{ path: `/admin/stores/${storeId}/products/${item.product_id}`, query: { return_to: route.fullPath } }"><div class="merchant-product-cover"><img v-if="item.cover_image_url" :src="resolveApiAssetUrl(item.cover_image_url) || undefined" :alt="item.product_name" /><div v-else><span>暂无图片</span><small>进入商品补充款式图片</small></div><em :class="`status-${item.status}`">{{ statusLabel(item.status) }}</em><span class="merchant-card-edit-action">直接编辑商品</span></div><div class="merchant-product-card-body"><h2>{{ item.product_name }}</h2><div class="merchant-product-price"><strong>¥{{ item.min_price }}</strong><span v-if="item.min_price !== item.max_price">起</span></div><dl><div><dt>款式</dt><dd>{{ item.sku_count }}</dd></div><div><dt>库存</dt><dd>{{ item.available_quantity }}</dd></div><div><dt>销量</dt><dd>{{ item.sales_count }}</dd></div><div><dt>评价</dt><dd>★ {{ item.rating_score }}</dd></div></dl></div></RouterLink><button v-if="auth.has('products:update')" class="merchant-card-delete-action" type="button" @click="beginProductDelete(item)">删除商品</button><button v-if="item.status === 'on_sale' || item.status === 'off_shelf'" type="button" class="admin-product-status-toggle" :disabled="saving" @click="toggleProduct(item)">{{ item.status === 'on_sale' ? '下架商品' : '重新上架' }}</button><RouterLink v-else class="admin-product-status-toggle" :to="{ path: `/admin/stores/${storeId}/products/${item.product_id}`, query: { return_to: route.fullPath } }">处理{{ statusLabel(item.status) }}商品</RouterLink></article></div></PageState>
          <p class="admin-store-panel-footnote">当前载入可售库存 {{ totalStock }} 件。库存编辑、款式图片、详情、常见问题与评价处理均从具体商品进入。</p>
        </section>

        <section v-else class="admin-store-panel admin-store-orders-panel">
          <header><div><p class="eyebrow">STORE ORDERS</p><h2>该店铺的订单</h2><p>超级管理员负责监管和异常处置；页面中的“顾客”与“店铺”角色保持真实关系。</p></div></header>
          <section v-if="revenue" class="merchant-income-grid"><article class="primary"><span>总营业额</span><strong>{{ formatMoney(revenue.net_revenue) }}</strong><small>确认收货金额减退款</small></article><article><span>今日收益</span><strong>{{ formatMoney(revenue.today_revenue) }}</strong></article><article><span>昨日收益</span><strong>{{ formatMoney(revenue.yesterday_revenue) }}</strong></article><article><span>近 30 日收益</span><strong>{{ formatMoney(revenue.last_30_days_revenue) }}</strong></article></section>
          <nav class="merchant-order-tabs"><button v-for="tab in orderTabs" :key="tab.value" type="button" :class="{ active: orderView === tab.value, urgent: tab.value === 'after_sale' && tab.count > 0 }" @click="chooseOrderView(tab.value)"><span>{{ tab.label }}</span><b>{{ tab.count }}</b></button></nav>
          <PageState :loading="panelLoading" :error="''" :empty="!orders.length" empty-title="当前分类没有订单" @retry="loadOrders"><div class="merchant-order-list"><article v-for="item in orders" :key="item.order.order_id" class="merchant-order-card" :class="{ aftersale: item.order.after_sale_status === 'in_progress' }"><header><div><strong>{{ orderStatusLabel(item) }}</strong><small>{{ dateTime(item.order.created_at) }} · 订单 {{ item.order.order_id }}</small></div><span>顾客 {{ item.user_name_masked }}</span></header><div class="merchant-order-lines"><div v-for="line in item.order.items" :key="line.order_item_id"><img v-if="line.image_url" :src="resolveApiAssetUrl(line.image_url) || undefined" alt="" /><div v-else class="order-image-placeholder">商品</div><span><strong>{{ line.product_name }}</strong><small>{{ line.sku_name }} · × {{ line.quantity }}</small></span><b>{{ formatMoney(line.payable_amount) }}</b></div></div><footer><span>实付 {{ formatMoney(item.order.amounts.paid_amount) }}<small v-if="item.order.amounts.refunded_amount.minor_units !== '0'">已退款 {{ formatMoney(item.order.amounts.refunded_amount) }}</small></span><div class="actions"><RouterLink class="button-link secondary" :to="{ path: `/admin/orders/${item.order.order_id}`, query: { return_to: route.fullPath } }">监管订单详情</RouterLink></div></footer></article></div><button v-if="orderNextCursor" type="button" class="secondary admin-store-orders-more" :disabled="orderLoadingMore" @click="loadMoreOrders">{{ orderLoadingMore ? '正在加载…' : '加载更多订单' }}</button></PageState>
        </section>
      </template>
    </PageState>
    <Teleport to="body"><div v-if="statusConfirmOpen && store" class="admin-form-overlay" @click.self="statusConfirmOpen = false"><section class="admin-form-dialog admin-store-status-confirm" role="dialog" aria-modal="true" aria-labelledby="admin-store-status-title"><header><div><p class="eyebrow">营业状态确认</p><h2 id="admin-store-status-title">{{ store.status === 'active' ? '确认暂停该店铺营业？' : '确认恢复该店铺营业？' }}</h2><p>{{ store.status === 'active' ? '暂停后，顾客仍能查看历史订单，但不能从该店铺产生新的购买。' : '恢复后，该店铺销售中的商品将重新允许顾客浏览和购买。' }}</p></div><button type="button" aria-label="关闭" @click="statusConfirmOpen = false">×</button></header><footer><button type="button" class="secondary" @click="statusConfirmOpen = false">取消</button><button type="button" :class="store.status === 'active' ? 'danger' : ''" :disabled="saving" @click="toggleStoreStatus">{{ store.status === 'active' ? '确认暂停营业' : '确认恢复营业' }}</button></footer></section></div></Teleport>
    <Teleport to="body"><div v-if="deletingProduct" class="merchant-delete-overlay" @mousedown.self="closeProductDeletion"><section class="merchant-delete-dialog" role="alertdialog" aria-modal="true" aria-labelledby="admin-product-delete-title"><span>{{ checkingProductDeletion ? '…' : productDeletionEligibility?.has_transactions ? '↘' : '!' }}</span><template v-if="checkingProductDeletion"><h2 id="admin-product-delete-title">正在检查商品交易记录</h2><p>请稍候，系统正在确认这件商品是否允许删除。</p><div class="actions"><button type="button" class="secondary" @click="closeProductDeletion">取消</button></div></template><template v-else-if="productDeletionEligibility?.has_transactions"><h2 id="admin-product-delete-title">“{{ deletingProduct.product_name }}”已有交易，不能删除</h2><p>{{ productDeletionEligibility.message }} 为了保留顾客订单、退款、售后和审计记录，历史商品数据必须继续存在。</p><div class="actions"><button type="button" class="secondary" @click="closeProductDeletion">取消</button><button v-if="productDeletionEligibility.can_off_shelf" type="button" class="danger" :disabled="productDeletionBusy" @click="confirmProductOffShelf">{{ productDeletionBusy ? '正在下架…' : '下架商品' }}</button><button v-else type="button" class="secondary" disabled>{{ productDeletionEligibility.current_status === 'off_shelf' ? '商品已下架' : '当前不能下架' }}</button></div></template><template v-else-if="productDeletionEligibility?.can_delete"><h2 id="admin-product-delete-title">“{{ deletingProduct.product_name }}”没有产生过交易</h2><p>系统没有发现该商品的订单交易记录，可以直接删除。删除后商品会从顾客端、商家端和超级管理端消失，且不能恢复。</p><div class="actions"><button type="button" class="secondary" @click="closeProductDeletion">取消</button><button type="button" class="danger" :disabled="productDeletionBusy" @click="confirmProductDelete">{{ productDeletionBusy ? '正在删除…' : '直接删除' }}</button></div></template></section></div></Teleport>
    <Teleport to="body"><div v-if="deleteOpen" class="admin-form-overlay" @click.self="deleteOpen = false"><form class="admin-form-dialog" @submit.prevent="deleteStore"><header><div><p class="eyebrow">DANGER ZONE</p><h2>删除“{{ store?.store_name }}”及商家账号</h2><p>只有从未产生交易的店铺可以物理删除；已有订单时服务端会阻断，请改用“暂停营业”。</p></div><button type="button" @click="deleteOpen = false">×</button></header><label>删除原因<textarea v-model.trim="deleteReason" required minlength="2" maxlength="500" /></label><label>输入 DELETE_STORE 确认<input v-model.trim="deleteConfirmation" required /></label><footer><button type="button" class="secondary" @click="deleteOpen = false">取消</button><button class="danger" :disabled="saving || deleteConfirmation !== 'DELETE_STORE' || deleteReason.length < 2">{{ saving ? '正在删除…' : '永久删除无交易店铺' }}</button></footer></form></div></Teleport>
  </section>
</template>
