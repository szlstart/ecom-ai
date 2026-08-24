<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  getStore,
  getStoreGroups,
  getStoreHome,
  getStorePolicies,
  getStoreProducts,
  setStoreFollow,
  type ProductCardData,
  type StoreData,
  type StoreGroup,
  type StoreHomeContent,
  type StorePolicy,
} from '@/api/catalog'
import { errorMessage, resolveApiAssetUrl, type PaginationMeta } from '@/api/http'
import { ensureStoreConversation, setConversationContext } from '@/api/messaging'
import PageState from '@/components/PageState.vue'
import ProductCard from '@/components/ProductCard.vue'
import { useUserAuthStore } from '@/stores/user-auth'

const route = useRoute()
const router = useRouter()
const auth = useUserAuthStore()
const store = ref<StoreData | null>(null)
const home = ref<StoreHomeContent | null>(null)
const groups = ref<StoreGroup[]>([])
const policies = ref<StorePolicy[]>([])
const products = ref<ProductCardData[]>([])
const pagination = ref<PaginationMeta | null>(null)
const q = ref('')
const groupId = ref('')
const sort = ref('relevance')
const loading = ref(true)
const productLoading = ref(false)
const error = ref('')
const partialWarning = ref('')
const followBusy = ref(false)
const contactBusy = ref(false)

function one(value: unknown): string { return typeof value === 'string' ? value : '' }
function storeId(): string { return String(route.params.storeId) }

async function loadStore() {
  loading.value = true
  error.value = ''
  partialWarning.value = ''
  const results = await Promise.allSettled([
    getStore(storeId(), auth.accessToken),
    getStoreHome(storeId(), auth.accessToken),
    getStoreGroups(storeId()),
    getStorePolicies(storeId()),
  ])
  const [storeResult, homeResult, groupResult, policyResult] = results
  if (storeResult.status === 'rejected') {
    error.value = errorMessage(storeResult.reason)
  } else {
    store.value = storeResult.value.data
    document.title = store.value.store_name
  }
  if (homeResult.status === 'fulfilled') home.value = homeResult.value.data
  if (groupResult.status === 'fulfilled') groups.value = groupResult.value.data.items
  if (policyResult.status === 'fulfilled') policies.value = policyResult.value.data.items
  if (results.slice(1).some((item) => item.status === 'rejected')) partialWarning.value = '部分店铺内容暂时不可用，商品列表仍可正常浏览。'
  loading.value = false
}

async function loadProducts() {
  productLoading.value = true
  try {
    const response = await getStoreProducts(storeId(), {
      q: one(route.query.q) || undefined,
      group_id: one(route.query.group_id) || undefined,
      sort: one(route.query.sort) || 'relevance',
      cursor: one(route.query.cursor) || undefined,
      limit: 20,
    }, auth.accessToken)
    products.value = response.data.items
    pagination.value = response.meta.pagination
  } catch (cause) {
    partialWarning.value = errorMessage(cause)
    products.value = []
  } finally {
    productLoading.value = false
  }
}

async function contactStore() {
  if (!store.value) return
  if (!auth.accessToken) {
    await router.push({ path: '/login', query: { redirect: route.fullPath } })
    return
  }
  contactBusy.value = true
  partialWarning.value = ''
  try {
    const conversation = (await ensureStoreConversation(store.value.store_id, auth.accessToken)).data
    await setConversationContext(conversation.conversation_id, conversation.version, 'store', store.value.store_id, null, auth.accessToken)
    await router.push(`/messages/${conversation.conversation_id}`)
  } catch (cause) { partialWarning.value = errorMessage(cause) }
  finally { contactBusy.value = false }
}

function syncFilters() {
  q.value = one(route.query.q)
  groupId.value = one(route.query.group_id)
  sort.value = one(route.query.sort) || 'relevance'
}

function applyFilters() {
  void router.push({ path: route.path, query: Object.fromEntries(Object.entries({
    q: q.value.trim(), group_id: groupId.value, sort: sort.value === 'relevance' ? '' : sort.value,
  }).filter(([, value]) => value)) })
}

function changeCursor(cursor: string | null | undefined) {
  if (cursor) void router.push({ path: route.path, query: { ...route.query, cursor }, hash: '#store-products' })
}

async function toggleFollow() {
  if (!store.value) return
  if (!auth.accessToken) {
    await router.push({ path: '/login', query: { redirect: route.fullPath } })
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

onMounted(() => { syncFilters(); void Promise.all([loadStore(), loadProducts()]) })
watch(() => route.params.storeId, () => { syncFilters(); void Promise.all([loadStore(), loadProducts()]) })
watch(() => route.fullPath, (current, previous) => {
  if (current !== previous && String(route.params.storeId)) { syncFilters(); void loadProducts() }
})
watch(() => auth.accessToken, () => void Promise.all([loadStore(), loadProducts()]))
</script>

<template>
  <PageState :loading="loading" :error="error" @retry="loadStore">
    <div v-if="store" class="storefront-stack">
      <header class="store-header-card">
        <div class="store-identity">
          <img v-if="store.logo_url" :src="resolveApiAssetUrl(store.logo_url) || undefined" alt="" width="88" height="88" />
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
        <form class="store-filter" role="search" @submit.prevent="applyFilters">
          <label>店内搜索<input v-model="q" type="search" maxlength="100" placeholder="搜索本店商品" /></label>
          <label>商品分组<select v-model="groupId"><option value="">全部分组</option><option v-for="group in groups" :key="group.group_id" :value="group.group_id">{{ group.group_name }}（{{ group.visible_product_count }}）</option></select></label>
          <label>排序<select v-model="sort"><option value="relevance">综合</option><option value="sales">销量</option><option value="newest">最新</option><option value="price_asc">价格从低到高</option><option value="price_desc">价格从高到低</option></select></label>
          <button type="submit">应用</button>
        </form>
        <PageState :loading="productLoading" :empty="!productLoading && products.length === 0" empty-title="店铺暂无匹配商品" empty-detail="可以切换分组或尝试其他关键词。">
          <div class="product-grid"><ProductCard v-for="product in products" :key="product.product_id" :product="product" :return-to="route.fullPath" /></div>
          <nav v-if="pagination" class="pagination" aria-label="店铺商品分页"><button class="secondary" type="button" :disabled="!pagination.has_previous" @click="changeCursor(pagination.previous_cursor)">上一页</button><span>每页 {{ pagination.limit }} 件</span><button class="secondary" type="button" :disabled="!pagination.has_next" @click="changeCursor(pagination.next_cursor)">下一页</button></nav>
        </PageState>
      </section>

      <section v-if="policies.length">
        <div class="section-heading"><div><p class="eyebrow">当前有效版本</p><h2>店铺服务政策</h2></div></div>
        <div class="policy-grid"><details v-for="policy in policies" :key="policy.policy_id"><summary>{{ policy.title }}</summary><p>{{ policy.content }}</p><small>版本 {{ policy.policy_version }} · 生效于 {{ new Date(policy.effective_at).toLocaleDateString('zh-CN') }}</small></details></div>
      </section>
    </div>
  </PageState>
</template>
