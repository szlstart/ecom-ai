<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import { getHomepage, searchProducts, type HomepageData } from '@/api/catalog'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import ProductCard from '@/components/ProductCard.vue'
import { useUserAuthStore } from '@/stores/user-auth'

const auth = useUserAuthStore()
const homepage = ref<HomepageData | null>(null)
const loading = ref(true)
const error = ref('')
const loadingMore = ref(false)
const loadMoreError = ref('')
const loadMoreSentinel = ref<HTMLElement | null>(null)
const infiniteScrollSupported = ref(true)
let feedRequestVersion = 0
let loadMoreObserver: IntersectionObserver | null = null

async function load() {
  const requestVersion = ++feedRequestVersion
  loading.value = true
  loadingMore.value = false
  loadMoreError.value = ''
  error.value = ''
  try {
    const response = await getHomepage(auth.accessToken)
    if (requestVersion !== feedRequestVersion) return
    homepage.value = response.data
  } catch (cause) {
    if (requestVersion !== feedRequestVersion) return
    error.value = errorMessage(cause)
  } finally {
    if (requestVersion === feedRequestVersion) loading.value = false
  }
}

async function loadMoreRecommended() {
  const section = homepage.value?.sections.find((item) => item.section === 'recommended')
  const cursor = section?.next_cursor
  if (!section || !cursor || loading.value || loadingMore.value) return
  const requestVersion = feedRequestVersion
  loadingMore.value = true
  loadMoreError.value = ''
  try {
    const response = await searchProducts({ sort: 'random', recommendation_seed: section.recommendation_seed, cursor, limit: 12 }, auth.accessToken)
    if (requestVersion !== feedRequestVersion || cursor !== section.next_cursor) return
    const existingIds = new Set(section.items.map((product) => product.product_id))
    section.items.push(...response.data.items.filter((product) => !existingIds.has(product.product_id)))
    section.next_cursor = response.meta.pagination?.next_cursor ?? null
  } catch (cause) {
    if (requestVersion === feedRequestVersion) loadMoreError.value = errorMessage(cause)
  } finally {
    if (requestVersion === feedRequestVersion) {
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

function setLoadMoreSentinel(element: unknown) {
  loadMoreSentinel.value = element instanceof HTMLElement ? element : null
}

function text(record: Record<string, unknown>, key: string): string {
  const value = record[key]
  return typeof value === 'string' ? value : ''
}

onMounted(() => {
  if ('IntersectionObserver' in window) {
    loadMoreObserver = new IntersectionObserver((entries) => {
      if (!loadMoreError.value && entries.some((entry) => entry.isIntersecting)) void loadMoreRecommended()
    }, { rootMargin: '500px 0px' })
    if (loadMoreSentinel.value) loadMoreObserver.observe(loadMoreSentinel.value)
  } else {
    infiniteScrollSupported.value = false
  }
  void load()
})
watch(loadMoreSentinel, (current, previous) => {
  if (previous) loadMoreObserver?.unobserve(previous)
  if (current) loadMoreObserver?.observe(current)
})
watch(() => auth.accessToken, (current, previous) => {
  if (current !== previous) void load()
})
onBeforeUnmount(() => loadMoreObserver?.disconnect())
</script>

<template>
  <PageState :loading="loading" :error="error" @retry="load">
    <div v-if="homepage" class="storefront-stack home-page-stack">
      <section class="commerce-hero">
        <div>
          <p class="eyebrow">可信商品 · 安全交易</p>
          <h1>找到真正适合你的商品</h1>
          <p>浏览平台已发布商品，库存、款式与店铺政策均来自实时业务数据。</p>
          <RouterLink class="button-link" to="/search">开始选购</RouterLink>
        </div>
        <div v-if="homepage.banners.length" class="banner-copy" aria-label="首页推荐">
          <strong>{{ text(homepage.banners[0]!, 'title') || '本周精选' }}</strong>
          <span>{{ text(homepage.banners[0]!, 'subtitle') || '发现平台优质新品' }}</span>
        </div>
      </section>

      <aside v-if="homepage.announcements.length" class="announcement-strip" aria-label="平台公告">
        <strong>公告</strong>
        <span>{{ text(homepage.announcements[0]!, 'title') || text(homepage.announcements[0]!, 'content') }}</span>
      </aside>

      <section v-for="section in homepage.sections" :key="section.section" :class="{ 'home-recommended-section': section.section === 'recommended' }" :aria-labelledby="`section-${section.section}`">
        <div class="section-heading">
          <div><h2 :id="`section-${section.section}`">{{ section.title }}</h2></div>
          <RouterLink to="/search">更多商品</RouterLink>
        </div>
        <p v-if="section.status === 'unavailable'" class="alert warning" role="status">
          该推荐区域暂时不可用，其他区域仍可正常浏览。
        </p>
        <div v-else-if="section.items.length" class="product-grid">
          <ProductCard v-for="product in section.items" :key="product.product_id" :product="product" return-to="/" />
        </div>
        <p v-else class="muted">这个区域暂时没有已发布商品。</p>
        <div v-if="section.status === 'available' && section.items.length" :ref="setLoadMoreSentinel" class="infinite-scroll-status" role="status" aria-live="polite">
          <span v-if="loadingMore" class="infinite-loading-line"><span class="spinner" aria-hidden="true"></span>正在加载更多商品…</span>
          <button v-else-if="loadMoreError" type="button" class="secondary small" @click="loadMoreRecommended">加载失败，点击重试</button>
          <button v-else-if="section.next_cursor && !infiniteScrollSupported" type="button" class="secondary small" @click="loadMoreRecommended">加载更多商品</button>
          <span v-else-if="section.next_cursor">继续向下浏览，将自动加载更多商品</span>
          <span v-else class="infinite-scroll-end">已经到底了，共展示 {{ section.items.length }} 件商品</span>
        </div>
      </section>
    </div>
  </PageState>
</template>
