<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  getBrands,
  getCategories,
  getSuggestions,
  searchProducts,
  type Brand,
  type Category,
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
const categoryId = ref('')
const brandId = ref('')
const priceMin = ref('')
const priceMax = ref('')
const sort = ref('relevance')
const categories = ref<Category[]>([])
const brands = ref<Brand[]>([])
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
  categoryId.value = one(route.query.category_id)
  brandId.value = one(route.query.brand_id)
  priceMin.value = one(route.query.price_min)
  priceMax.value = one(route.query.price_max)
  sort.value = one(route.query.sort) || 'relevance'
}

async function loadProducts() {
  const version = ++requestVersion
  loading.value = true
  error.value = ''
  try {
    const response = await searchProducts({
      q: one(route.query.q) || undefined,
      category_id: one(route.query.category_id) || undefined,
      brand_id: one(route.query.brand_id) || undefined,
      price_min: one(route.query.price_min) || undefined,
      price_max: one(route.query.price_max) || undefined,
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

async function loadFilters() {
  const [categoryResult, brandResult] = await Promise.allSettled([getCategories(), getBrands()])
  if (categoryResult.status === 'fulfilled') categories.value = categoryResult.value.data
  if (brandResult.status === 'fulfilled') brands.value = brandResult.value.data
}

function submitSearch() {
  suggestions.value = []
  void router.push({
    path: '/search',
    query: compact({
      q: q.value.trim(),
      category_id: categoryId.value,
      brand_id: brandId.value,
      price_min: priceMin.value,
      price_max: priceMax.value,
      sort: sort.value === 'relevance' ? '' : sort.value,
    }),
  })
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
  void Promise.all([loadProducts(), loadFilters()])
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
      <span class="muted">筛选条件保存在地址栏，可复制或返回恢复</span>
    </header>

    <form class="search-panel" role="search" @submit.prevent="submitSearch">
      <div class="suggestion-field">
        <label>关键词<input v-model="q" type="search" maxlength="100" autocomplete="off" placeholder="商品名称、品牌或关键词" @input="scheduleSuggestions" /></label>
        <ul v-if="suggestions.length" class="suggestion-list" aria-label="搜索建议">
          <li v-for="item in suggestions" :key="item"><button type="button" @click="selectSuggestion(item)">{{ item }}</button></li>
        </ul>
      </div>
      <label>分类<select v-model="categoryId"><option value="">全部分类</option><option v-for="item in categories" :key="item.category_id" :value="item.category_id">{{ item.category_name }}</option></select></label>
      <label>品牌<select v-model="brandId"><option value="">全部品牌</option><option v-for="item in brands" :key="item.brand_id" :value="item.brand_id">{{ item.brand_name }}</option></select></label>
      <label>最低价（分）<input v-model="priceMin" inputmode="numeric" pattern="[0-9]*" placeholder="0" /></label>
      <label>最高价（分）<input v-model="priceMax" inputmode="numeric" pattern="[0-9]*" placeholder="不限" /></label>
      <label>排序<select v-model="sort"><option value="relevance">综合</option><option value="sales">销量</option><option value="newest">最新</option><option value="price_asc">价格从低到高</option><option value="price_desc">价格从高到低</option></select></label>
      <button type="submit">应用筛选</button>
    </form>

    <div id="search-results" tabindex="-1">
      <PageState
        :loading="loading"
        :error="error"
        :empty="!loading && !error && products.length === 0"
        empty-title="没有找到匹配商品"
        empty-detail="可以减少筛选条件，或尝试其他关键词。"
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
