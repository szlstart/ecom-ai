<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { adminCommand, adminDelete, adminGet, adminQuery, requireAdminToken, type AdminProductDeletionEligibility, type AdminProductSummary, type AdminStore } from '@/api/admin-catalog'
import { ApiProblem, errorMessage, resolveApiAssetUrl } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const route = useRoute()
const router = useRouter()
const items = ref<AdminProductSummary[]>([])
const store = ref<AdminStore | null>(null)
const nextCursor = ref<string | null>(null)
const loading = ref(true)
const error = ref('')
const notice = ref('')
const q = ref('')
const status = ref('')
const deletingItem = ref<AdminProductSummary | null>(null)
const deleting = ref(false)
const checkingDeletion = ref(false)
const deletionEligibility = ref<AdminProductDeletionEligibility | null>(null)
const statusOptions = [
  ['', '全部'], ['on_sale', '销售中'], ['draft', '草稿'], ['pending_review', '审核中'], ['rejected', '需修改'], ['off_shelf', '已下架'],
] as const
const totalStock = computed(() => items.value.reduce((total, item) => total + item.available_quantity, 0))
const totalSales = computed(() => items.value.reduce((total, item) => total + item.sales_count, 0))

function one(value: unknown) { return typeof value === 'string' ? value : '' }
function statusLabel(value: string) { return ({ draft: '草稿', pending_review: '审核中', approved: '待上架', rejected: '需修改', on_sale: '销售中', off_shelf: '已下架' } as Record<string, string>)[value] ?? value }
function sync() { q.value = one(route.query.q); status.value = one(route.query.status) }

async function load() {
  loading.value = true; error.value = ''
  try {
    const [productsResult, storesResult] = await Promise.all([
      adminGet<{ items: AdminProductSummary[]; next_cursor: string | null }>(
        `/admin/products${adminQuery({ q: one(route.query.q), status: one(route.query.status), cursor: one(route.query.cursor), limit: 50 })}`,
        requireAdminToken(auth.accessToken),
      ),
      adminGet<{ items: AdminStore[]; next_cursor: string | null }>('/admin/stores?limit=20', requireAdminToken(auth.accessToken)),
    ])
    items.value = productsResult.data.items
    nextCursor.value = productsResult.data.next_cursor
    store.value = storesResult.data.items[0] ?? null
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

function applySearch() {
  void router.push({ path: '/merchant/products', query: Object.fromEntries(Object.entries({ q: q.value.trim(), status: status.value }).filter(([, value]) => value)) })
}
function chooseStatus(value: string) { status.value = value; applySearch() }
function next() { if (nextCursor.value) void router.push({ path: '/merchant/products', query: { ...route.query, cursor: nextCursor.value } }) }
function closeDeletion() { if (!deleting.value) { deletingItem.value = null; deletionEligibility.value = null } }
async function beginDelete(item: AdminProductSummary) {
  deletingItem.value = item; deletionEligibility.value = null; checkingDeletion.value = true; error.value = ''; notice.value = ''
  try {
    deletionEligibility.value = (await adminGet<AdminProductDeletionEligibility>(`/admin/products/${encodeURIComponent(item.product_id)}/deletion-eligibility`, requireAdminToken(auth.accessToken))).data
  } catch (cause) { error.value = errorMessage(cause); deletingItem.value = null }
  finally { checkingDeletion.value = false }
}
async function confirmDelete() {
  if (!deletingItem.value || !deletionEligibility.value?.can_delete) return
  deleting.value = true; error.value = ''
  try {
    await adminDelete(`/admin/products/${encodeURIComponent(deletingItem.value.product_id)}`, requireAdminToken(auth.accessToken), deletingItem.value.version, 'merchant-product-delete')
    deletingItem.value = null
    deletionEligibility.value = null
    notice.value = '该商品没有产生过交易，已直接删除。'
    await load()
  } catch (cause) {
    if (cause instanceof ApiProblem && cause.body.code === 'PRODUCT_HAS_TRANSACTIONS' && deletingItem.value) {
      deletionEligibility.value = { product_id: deletingItem.value.product_id, current_status: deletingItem.value.status, has_transactions: true, can_delete: false, can_off_shelf: deletingItem.value.status === 'on_sale', recommended_action: deletingItem.value.status === 'on_sale' ? 'off_shelf' : 'none', message: cause.body.detail }
    } else { error.value = errorMessage(cause); deletingItem.value = null; deletionEligibility.value = null }
  }
  finally { deleting.value = false }
}
async function confirmOffShelf() {
  if (!deletingItem.value || !deletionEligibility.value?.can_off_shelf) return
  deleting.value = true; error.value = ''
  try {
    await adminCommand(`/admin/products/${encodeURIComponent(deletingItem.value.product_id)}/off-shelf-commands`, { reason_code: 'HAS_TRANSACTION_DELETE_GUARD', reason: '商品已有交易记录，商家在删除提示中选择下架。' }, requireAdminToken(auth.accessToken), deletingItem.value.version, 'merchant-product-off-shelf')
    deletingItem.value = null; deletionEligibility.value = null
    notice.value = '商品已有交易记录，不能删除，现已下架。'
    await load()
  } catch (cause) { error.value = errorMessage(cause); deletingItem.value = null; deletionEligibility.value = null }
  finally { deleting.value = false }
}

onMounted(() => { sync(); void load() })
watch(() => route.fullPath, () => { sync(); void load() })
</script>

<template>
  <section class="merchant-page-stack merchant-shop-workbench">
    <p v-if="notice" class="alert success" aria-live="polite">{{ notice }}</p>
    <header class="merchant-shop-hero">
      <div class="merchant-shop-identity"><span class="merchant-shop-logo"><img v-if="store?.logo_url" :src="resolveApiAssetUrl(store.logo_url) || undefined" alt="" /><template v-else>{{ store?.store_name.slice(0, 1) || '店' }}</template></span><div><p class="eyebrow">我的店铺</p><h1>{{ store?.store_name || '商品管理' }}</h1><p>{{ store?.description || '把顾客会看到的商品，直接在这里编辑。' }}</p></div></div>
      <dl><div><dt>商品</dt><dd>{{ items.length }}</dd></div><div><dt>可售库存</dt><dd>{{ totalStock }}</dd></div><div><dt>累计销量</dt><dd>{{ totalSales }}</dd></div><div><dt>店铺评分</dt><dd>{{ store?.rating_score || '—' }}</dd></div></dl>
      <RouterLink v-if="store" class="merchant-store-preview-link" :to="`/stores/${store.store_id}`" target="_blank">打开顾客看到的店铺 ↗</RouterLink>
    </header>

    <div class="merchant-products-toolbar">
      <div class="merchant-segmented" aria-label="商品状态"><button v-for="entry in statusOptions" :key="entry[0] || 'all'" :class="{ active: status === entry[0] }" type="button" @click="chooseStatus(entry[0])">{{ entry[1] }}</button></div>
      <form class="merchant-product-search" @submit.prevent="applySearch"><input v-model="q" maxlength="255" placeholder="搜索商品名称" aria-label="搜索商品名称" /><button>搜索</button></form>
    </div>

    <PageState :loading="loading" :error="error" :empty="false" @retry="load">
      <div class="merchant-product-grid">
        <RouterLink v-if="status === ''" class="merchant-product-card merchant-add-product" to="/merchant/products/new"><span>＋</span><strong>新增商品</strong><small>直接进入可编辑的商品详情</small></RouterLink>
        <article v-for="item in items" :key="item.product_id" class="merchant-product-card merchant-manage-card">
          <RouterLink class="merchant-product-card-link" :to="`/merchant/products/${item.product_id}`"><div class="merchant-product-cover"><img v-if="item.cover_image_url" :src="resolveApiAssetUrl(item.cover_image_url) || undefined" :alt="item.product_name" /><div v-else><span>暂无图片</span><small>点击进入上传商品图</small></div><em :class="`status-${item.status}`">{{ statusLabel(item.status) }}</em><span class="merchant-card-edit-action">编辑商品</span></div><div class="merchant-product-card-body"><h2>{{ item.product_name }}</h2><p>{{ item.subtitle || '暂未填写商品卖点' }}</p><div class="merchant-product-price"><strong>¥{{ item.min_price }}</strong><span v-if="item.min_price !== item.max_price">起</span></div><dl><div><dt>款式</dt><dd>{{ item.sku_count }}</dd></div><div><dt>可售</dt><dd>{{ item.available_quantity }}</dd></div><div><dt>销量</dt><dd>{{ item.sales_count }}</dd></div><div><dt>评价</dt><dd>★ {{ item.rating_score }} · {{ item.review_count }}</dd></div></dl></div></RouterLink>
          <button class="merchant-card-delete-action" type="button" @click="beginDelete(item)">删除商品</button>
        </article>
      </div>
      <p v-if="!loading && !error && items.length === 0" class="merchant-first-product-tip">{{ status === '' ? '还没有商品。点击左上角的“新增商品”，发布你的第一件商品吧。' : `当前没有“${statusOptions.find((entry) => entry[0] === status)?.[1] || '该状态'}”商品。` }}</p>
      <nav v-if="route.query.cursor || nextCursor" class="pagination"><button type="button" class="secondary" :disabled="!route.query.cursor" @click="router.back()">上一页</button><button type="button" class="secondary" :disabled="!nextCursor" @click="next">下一页</button></nav>
    </PageState>
    <Teleport to="body"><div v-if="deletingItem" class="merchant-delete-overlay" @mousedown.self="closeDeletion"><section class="merchant-delete-dialog" role="alertdialog" aria-modal="true" aria-labelledby="merchant-delete-title"><span>{{ checkingDeletion ? '…' : deletionEligibility?.has_transactions ? '↘' : '!' }}</span><template v-if="checkingDeletion"><h2 id="merchant-delete-title">正在检查商品交易记录</h2><p>请稍候，系统正在确认这件商品是否允许删除。</p><div class="actions"><button type="button" class="secondary" @click="closeDeletion">取消</button></div></template><template v-else-if="deletionEligibility?.has_transactions"><h2 id="merchant-delete-title">“{{ deletingItem.product_name }}”已有交易，不能删除</h2><p>{{ deletionEligibility.message }} 为了保证顾客订单、退款、售后和审计记录完整，历史商品数据必须继续保留。</p><div class="actions"><button type="button" class="secondary" @click="closeDeletion">取消</button><button v-if="deletionEligibility.can_off_shelf" type="button" class="danger" :disabled="deleting" @click="confirmOffShelf">{{ deleting ? '正在下架…' : '下架商品' }}</button><button v-else type="button" class="secondary" disabled>{{ deletionEligibility.current_status === 'off_shelf' ? '商品已下架' : '当前不能下架' }}</button></div></template><template v-else-if="deletionEligibility?.can_delete"><h2 id="merchant-delete-title">“{{ deletingItem.product_name }}”没有产生过交易</h2><p>系统没有发现该商品的订单交易记录，可以直接删除。删除后商品会从顾客端和商家商品列表消失，且不能恢复。</p><div class="actions"><button type="button" class="secondary" @click="closeDeletion">取消</button><button type="button" class="danger" :disabled="deleting" @click="confirmDelete">{{ deleting ? '正在删除…' : '直接删除' }}</button></div></template></section></div></Teleport>
  </section>
</template>
