<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { LocationQueryRaw } from 'vue-router'

import {
  getSuggestions,
  searchProducts,
  type ProductCardData,
} from '@/api/catalog'
import { errorMessage, type PaginationMeta } from '@/api/http'
import PageState from '@/components/PageState.vue'
import ProductCard from '@/components/ProductCard.vue'
import { useUserAuthStore } from '@/stores/user-auth'

const route = useRoute()
const router = useRouter()
const auth = useUserAuthStore()
const q = ref('')
const sort = ref('relevance')
const products = ref<ProductCardData[]>([])
const suggestions = ref<string[]>([])
const pagination = ref<PaginationMeta | null>(null)
const loading = ref(true)
const error = ref('')
let suggestionTimer: ReturnType<typeof setTimeout> | undefined
let requestVersion = 0

function one(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function syncForm() {
  q.value = one(route.query.q)
  sort.value = one(route.query.sort) || 'relevance'
}

async function loadProducts() {
  const version = ++requestVersion
  loading.value = true
  error.value = ''
  try {
    const response = await searchProducts({
      q: one(route.query.q) || undefined,
      sort: one(route.query.sort) || 'relevance',
      cursor: one(route.query.cursor) || undefined,
      limit: 20,
    }, auth.accessToken)
    if (version !== requestVersion) return
    products.value = response.data.items
    pagination.value = response.meta.pagination
  } catch (cause) {
    if (version === requestVersion) error.value = errorMessage(cause)
  } finally {
    if (version === requestVersion) loading.value = false
  }
}

function submitSearch() {
  suggestions.value = []
  void router.push({
    path: '/search',
    query: compact({
      q: q.value.trim(),
      sort: sort.value === 'relevance' ? '' : sort.value,
    }),
  })
}

function applySort(nextSort: string) {
  if (sort.value === nextSort && !route.query.cursor) return
  sort.value = nextSort
  const query: LocationQueryRaw = { ...route.query, sort: nextSort === 'relevance' ? undefined : nextSort }
  delete query.cursor
  if (!query.sort) delete query.sort
  void router.push({ path: '/search', query })
}

function changeCursor(cursor: string | null | undefined) {
  if (!cursor) return
  void router.push({ path: '/search', query: { ...route.query, cursor }, hash: '#search-results' })
}

function scheduleSuggestions() {
  if (suggestionTimer) clearTimeout(suggestionTimer)
  const term = q.value.trim()
  if (!term) {
    suggestions.value = []
    return
  }
  suggestionTimer = setTimeout(async () => {
    try {
      suggestions.value = (await getSuggestions(term)).data.items
    } catch {
      suggestions.value = []
    }
  }, 250)
}

function selectSuggestion(value: string) {
  q.value = value
  submitSearch()
}

function compact(values: Record<string, string>) {
  return Object.fromEntries(Object.entries(values).filter(([, value]) => value !== ''))
}

onMounted(() => {
  syncForm()
  void loadProducts()
})
watch(() => route.fullPath, () => {
  syncForm()
  void loadProducts()
})
watch(() => auth.accessToken, () => void loadProducts())
onBeforeUnmount(() => { if (suggestionTimer) clearTimeout(suggestionTimer) })
</script>

<template>
  <section class="storefront-stack">
    <header class="page-heading">
      <div><p class="eyebrow">商品搜索</p><h1>搜索商品</h1></div>
      <span class="muted">搜索词与排序方式保存在地址栏，可复制或返回恢复</span>
    </header>

    <form class="search-panel" role="search" @submit.prevent="submitSearch">
      <div class="suggestion-field">
        <label>关键词<input v-model="q" type="search" maxlength="100" autocomplete="off" placeholder="商品名称、品牌或关键词" @input="scheduleSuggestions" /></label>
        <ul v-if="suggestions.length" class="suggestion-list" aria-label="搜索建议">
          <li v-for="item in suggestions" :key="item"><button type="button" @click="selectSuggestion(item)">{{ item }}</button></li>
        </ul>
      </div>
      <button type="submit">搜索</button>
    </form>

    <div class="sort-bar" aria-label="商品排序">
      <span>排序</span>
      <button type="button" :class="{ active: sort === 'relevance' }" :aria-pressed="sort === 'relevance'" @click="applySort('relevance')">综合排序</button>
      <button type="button" :class="{ active: sort === 'sales' }" :aria-pressed="sort === 'sales'" @click="applySort('sales')">销量排序</button>
      <button type="button" :class="{ active: sort === 'newest' }" :aria-pressed="sort === 'newest'" @click="applySort('newest')">最新</button>
      <button type="button" :class="{ active: sort === 'price_asc' }" :aria-pressed="sort === 'price_asc'" @click="applySort('price_asc')">价格从低到高</button>
      <button type="button" :class="{ active: sort === 'price_desc' }" :aria-pressed="sort === 'price_desc'" @click="applySort('price_desc')">价格从高到低</button>
    </div>

    <div id="search-results" tabindex="-1">
      <PageState
        :loading="loading"
        :error="error"
        :empty="!loading && !error && products.length === 0"
        empty-title="没有找到匹配商品"
        empty-detail="可以尝试其他关键词。"
        @retry="loadProducts"
      >
        <div class="product-grid">
          <ProductCard v-for="product in products" :key="product.product_id" :product="product" :return-to="route.fullPath" />
        </div>
        <nav v-if="pagination" class="pagination" aria-label="搜索结果分页">
          <button class="secondary" type="button" :disabled="!pagination.has_previous" @click="changeCursor(pagination.previous_cursor)">上一页</button>
          <span>每页 {{ pagination.limit }} 件</span>
          <button class="secondary" type="button" :disabled="!pagination.has_next" @click="changeCursor(pagination.next_cursor)">下一页</button>
        </nav>
      </PageState>
    </div>
  </section>
</template>
