<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  getStore,
  getStoreHome,
  getStorePolicies,
  getStoreProducts,
  setStoreFollow,
  type ProductCardData,
  type StoreData,
  type StoreHomeContent,
  type StorePolicy,
} from '@/api/catalog'
import { errorMessage, resolveApiAssetUrl } from '@/api/http'
import { ensureStoreConversation, setConversationContext } from '@/api/messaging'
import PageState from '@/components/PageState.vue'
import ProductCard from '@/components/ProductCard.vue'
import { useUserAuthStore } from '@/stores/user-auth'
import { useMessageCenterStore } from '@/stores/message-center'

const route = useRoute()
const router = useRouter()
const auth = useUserAuthStore()
const messageCenter = useMessageCenterStore()
const store = ref<StoreData | null>(null)
const home = ref<StoreHomeContent | null>(null)
const policies = ref<StorePolicy[]>([])
const products = ref<ProductCardData[]>([])
const nextCursor = ref<string | null>(null)
const loadMoreSentinel = ref<HTMLElement | null>(null)
const q = ref('')
const sort = ref('relevance')
const loading = ref(true)
const productLoading = ref(false)
const loadingMore = ref(false)
const loadMoreError = ref('')
const infiniteScrollSupported = ref(true)
const error = ref('')
const partialWarning = ref('')
const followBusy = ref(false)
const contactBusy = ref(false)
const sortOptions = [
  { value: 'relevance', label: '综合排序' },
  { value: 'sales', label: '销量排序' },
  { value: 'newest', label: '最新' },
  { value: 'price_asc', label: '价格从低到高' },
  { value: 'price_desc', label: '价格从高到低' },
] as const
let productRequestVersion = 0
let loadMoreObserver: IntersectionObserver | null = null

function one(value: unknown): string { return typeof value === 'string' ? value : '' }
function storeId(): string { return String(route.params.storeId) }

async function loadStore() {
  loading.value = true
  error.value = ''
  partialWarning.value = ''
  const results = await Promise.allSettled([
    getStore(storeId(), auth.accessToken),
    getStoreHome(storeId(), auth.accessToken),
    getStorePolicies(storeId()),
  ])
  const [storeResult, homeResult, policyResult] = results
  if (storeResult.status === 'rejected') {
    error.value = errorMessage(storeResult.reason)
  } else {
    store.value = storeResult.value.data
    document.title = store.value.store_name
  }
  if (homeResult.status === 'fulfilled') home.value = homeResult.value.data
  if (policyResult.status === 'fulfilled') policies.value = policyResult.value.data.items
  if (results.slice(1).some((item) => item.status === 'rejected')) partialWarning.value = '部分店铺内容暂时不可用，商品列表仍可正常浏览。'
  loading.value = false
}

async function loadProducts() {
  const requestVersion = ++productRequestVersion
  productLoading.value = true
  loadingMore.value = false
  loadMoreError.value = ''
  nextCursor.value = null
  try {
    const response = await getStoreProducts(storeId(), {
      q: one(route.query.q) || undefined,
      sort: one(route.query.sort) || 'relevance',
      limit: 20,
    }, auth.accessToken)
    if (requestVersion !== productRequestVersion) return
    products.value = response.data.items
    nextCursor.value = response.meta.pagination?.next_cursor ?? null
  } catch (cause) {
    if (requestVersion !== productRequestVersion) return
    partialWarning.value = errorMessage(cause)
    products.value = []
  } finally {
    if (requestVersion === productRequestVersion) productLoading.value = false
  }
}

async function loadMoreProducts() {
  const cursor = nextCursor.value
  if (!cursor || productLoading.value || loadingMore.value) return
  const requestVersion = productRequestVersion
  loadingMore.value = true
  loadMoreError.value = ''
  try {
    const response = await getStoreProducts(storeId(), {
      q: one(route.query.q) || undefined,
      sort: one(route.query.sort) || 'relevance',
      cursor,
      limit: 20,
    }, auth.accessToken)
    if (requestVersion !== productRequestVersion || cursor !== nextCursor.value) return
    const existingIds = new Set(products.value.map((product) => product.product_id))
    products.value.push(...response.data.items.filter((product) => !existingIds.has(product.product_id)))
    nextCursor.value = response.meta.pagination?.next_cursor ?? null
  } catch (cause) {
    if (requestVersion === productRequestVersion) loadMoreError.value = errorMessage(cause)
  } finally {
    if (requestVersion === productRequestVersion) {
      loadingMore.value = false
      await rearmLoadMoreObserver()
    }
  }
}

async function rearmLoadMoreObserver() {
  await nextTick()
  const sentinel = loadMoreSentinel.value
  if (!sentinel || !loadMoreObserver) return
  loadMoreObserver.unobserve(sentinel)
  loadMoreObserver.observe(sentinel)
}

async function contactStore() {
  if (!store.value) return
  if (!auth.accessToken) {
    await router.push({ path: route.path, query: { ...route.query, auth: 'login', redirect: route.fullPath } })
    return
  }
  contactBusy.value = true
  partialWarning.value = ''
  try {
    const conversation = (await ensureStoreConversation(store.value.store_id, auth.accessToken)).data
    await setConversationContext(conversation.conversation_id, conversation.version, 'store', store.value.store_id, null, auth.accessToken)
    messageCenter.show(conversation.conversation_id)
  } catch (cause) { partialWarning.value = errorMessage(cause) }
  finally { contactBusy.value = false }
}

function syncFilters() {
  q.value = one(route.query.q)
  sort.value = one(route.query.sort) || 'relevance'
}

function applyFilters() {
  void router.push({ path: route.path, query: Object.fromEntries(Object.entries({
    q: q.value.trim(), sort: sort.value === 'relevance' ? '' : sort.value,
  }).filter(([, value]) => value)) })
}

function changeSort(value: string) {
  if (sort.value === value) return
  sort.value = value
  applyFilters()
}

async function toggleFollow() {
  if (!store.value) return
  if (!auth.accessToken) {
    await router.push({ path: route.path, query: { ...route.query, auth: 'login', redirect: route.fullPath } })
    return
  }
  followBusy.value = true
  try {
    const next = !store.value.is_followed
    await setStoreFollow(store.value.store_id, next, auth.accessToken)
    store.value.is_followed = next
    store.value.follower_count += next ? 1 : -1
  } catch (cause) {
    partialWarning.value = errorMessage(cause)
  } finally {
    followBusy.value = false
  }
}

onMounted(() => {
  if ('IntersectionObserver' in window) {
    loadMoreObserver = new IntersectionObserver((entries) => {
      if (!loadMoreError.value && entries.some((entry) => entry.isIntersecting)) void loadMoreProducts()
    }, { rootMargin: '500px 0px' })
    if (loadMoreSentinel.value) loadMoreObserver.observe(loadMoreSentinel.value)
  } else {
    infiniteScrollSupported.value = false
  }
  syncFilters()
  void Promise.all([loadStore(), loadProducts()])
})
watch(loadMoreSentinel, (current, previous) => {
  if (previous) loadMoreObserver?.unobserve(previous)
  if (current) loadMoreObserver?.observe(current)
})
watch(
  () => [String(route.params.storeId), one(route.query.q), one(route.query.sort)] as const,
  (current, previous) => {
    if (!previous) return
    syncFilters()
    if (current[0] !== previous[0]) void Promise.all([loadStore(), loadProducts()])
    else void loadProducts()
  },
)
watch(() => auth.accessToken, (current, previous) => {
  if (current !== previous) void Promise.all([loadStore(), loadProducts()])
})
onBeforeUnmount(() => loadMoreObserver?.disconnect())
</script>

<template>
  <PageState :loading="loading" :error="error" @retry="loadStore">
    <div v-if="store" class="storefront-stack store-page-stack">
      <header class="store-header-card">
        <div class="store-identity">
          <img v-if="store.logo_url" :src="resolveApiAssetUrl(store.logo_url) || undefined" alt="" width="68" height="68" />
          <span v-else class="store-logo-placeholder" aria-hidden="true">店</span>
          <div><p class="eyebrow">认证店铺</p><h1>{{ store.store_name }}</h1><p class="muted">{{ store.description || '店铺暂未填写简介' }}</p></div>
        </div>
        <div class="store-actions">
          <button type="button" class="secondary" :disabled="followBusy || store.visibility_mode !== 'public'" @click="toggleFollow">{{ store.is_followed ? '已收藏' : '收藏店铺' }}</button>
          <button type="button" :disabled="contactBusy" @click="contactStore">{{ contactBusy ? '进入客服…' : '联系客服' }}</button>
        </div>
        <dl class="store-metrics"><div><dt>店铺评分</dt><dd>{{ store.rating_score }}</dd></div><div><dt>在售商品</dt><dd>{{ store.active_product_count }}</dd></div><div><dt>已售</dt><dd>{{ store.sales_count }}</dd></div><div><dt>收藏</dt><dd>{{ store.follower_count }}</dd></div></dl>
      </header>

      <p v-if="store.visibility_mode !== 'public'" class="alert warning">该店铺当前停止经营，仅保留历史订单相关信息和受限客服入口。</p>
      <p v-if="partialWarning" class="alert warning" role="status">{{ partialWarning }}</p>

      <section v-if="home?.announcements.length" class="announcement-strip" aria-label="店铺公告"><strong>店铺公告</strong><span>{{ home.announcements[0]?.title || home.announcements[0]?.content }}</span></section>
      <section v-if="home?.recommended_products.length">
        <div class="section-heading"><div><p class="eyebrow">店铺精选</p><h2>推荐商品</h2></div></div>
        <div class="product-grid compact-grid"><ProductCard v-for="product in home.recommended_products" :key="product.product_id" :product="product" :return-to="route.fullPath" /></div>
      </section>

      <section id="store-products">
        <div class="section-heading"><div><p class="eyebrow">全部在售</p><h2>店铺商品</h2></div></div>
        <div class="store-product-tools">
          <form class="store-filter" role="search" @submit.prevent="applyFilters">
            <label>店内搜索<input v-model="q" type="search" maxlength="100" placeholder="输入商品名称或关键词" /></label>
            <button class="store-search-button" type="submit">搜索</button>
          </form>
          <nav class="sort-bar store-sort-bar" aria-label="店铺商品排序">
            <span>排序</span>
            <button v-for="option in sortOptions" :key="option.value" type="button" :class="{ active: sort === option.value }" :aria-pressed="sort === option.value" @click="changeSort(option.value)">{{ option.label }}</button>
          </nav>
        </div>
        <PageState :loading="productLoading" :empty="!productLoading && products.length === 0" empty-title="店铺暂无匹配商品" empty-detail="可以尝试其他关键词或切换排序方式。">
          <div class="product-grid"><ProductCard v-for="product in products" :key="product.product_id" :product="product" :return-to="route.fullPath" /></div>
          <div ref="loadMoreSentinel" class="infinite-scroll-status" role="status" aria-live="polite">
            <span v-if="loadingMore" class="infinite-loading-line"><span class="spinner" aria-hidden="true"></span>正在加载更多商品…</span>
            <button v-else-if="loadMoreError" type="button" class="secondary small" @click="loadMoreProducts">加载失败，点击重试</button>
            <button v-else-if="nextCursor && !infiniteScrollSupported" type="button" class="secondary small" @click="loadMoreProducts">加载更多商品</button>
            <span v-else-if="nextCursor">继续向下浏览，将自动加载更多商品</span>
            <span v-else-if="products.length" class="infinite-scroll-end">已经到底了，共展示 {{ products.length }} 件商品</span>
          </div>
        </PageState>
      </section>

      <section v-if="policies.length">
        <div class="section-heading"><div><p class="eyebrow">当前有效版本</p><h2>店铺服务政策</h2></div></div>
        <div class="policy-grid"><details v-for="policy in policies" :key="policy.policy_id"><summary>{{ policy.title }}</summary><p>{{ policy.content }}</p><small>版本 {{ policy.policy_version }} · 生效于 {{ new Date(policy.effective_at).toLocaleDateString('zh-CN') }}</small></details></div>
      </section>
    </div>
  </PageState>
</template>
