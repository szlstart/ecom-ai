<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import { getHomepage, type HomepageData } from '@/api/catalog'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import ProductCard from '@/components/ProductCard.vue'
import { useUserAuthStore } from '@/stores/user-auth'

const auth = useUserAuthStore()
const homepage = ref<HomepageData | null>(null)
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    homepage.value = (await getHomepage(auth.accessToken)).data
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

function text(record: Record<string, unknown>, key: string): string {
  const value = record[key]
  return typeof value === 'string' ? value : ''
}

onMounted(load)
watch(() => auth.accessToken, (current, previous) => {
  if (current !== previous) void load()
})
</script>

<template>
  <PageState :loading="loading" :error="error" @retry="load">
    <div v-if="homepage" class="storefront-stack">
      <section class="commerce-hero">
        <div>
          <p class="eyebrow">可信商品 · 安全交易</p>
          <h1>找到真正适合你的商品</h1>
          <p>浏览平台已发布商品，库存、规格与店铺政策均来自实时业务数据。</p>
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

      <section v-for="section in homepage.sections" :key="section.section" :aria-labelledby="`section-${section.section}`">
        <div class="section-heading">
          <div><p class="eyebrow">{{ section.section.replace('_', ' ') }}</p><h2 :id="`section-${section.section}`">{{ section.title }}</h2></div>
          <RouterLink to="/search">更多商品</RouterLink>
        </div>
        <p v-if="section.status === 'unavailable'" class="alert warning" role="status">
          该推荐区域暂时不可用，其他区域仍可正常浏览。
        </p>
        <div v-else-if="section.items.length" class="product-grid">
          <ProductCard v-for="product in section.items" :key="product.product_id" :product="product" return-to="/" />
        </div>
        <p v-else class="muted">这个区域暂时没有已发布商品。</p>
      </section>
    </div>
  </PageState>
</template>
