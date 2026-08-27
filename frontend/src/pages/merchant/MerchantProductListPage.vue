<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { adminGet, adminQuery, requireAdminToken, type AdminProductSummary, type AdminStore } from '@/api/admin-catalog'
import { errorMessage, resolveApiAssetUrl } from '@/api/http'
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
const q = ref('')
const status = ref('')
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

onMounted(() => { sync(); void load() })
watch(() => route.fullPath, () => { sync(); void load() })
</script>

<template>
  <section class="merchant-page-stack merchant-shop-workbench">
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
        <RouterLink class="merchant-product-card merchant-add-product" to="/merchant/products/new"><span>＋</span><strong>新增商品</strong><small>从一张空白商品卡片开始</small></RouterLink>
        <RouterLink v-for="item in items" :key="item.product_id" class="merchant-product-card" :to="`/merchant/products/${item.product_id}`">
          <div class="merchant-product-cover"><img v-if="item.cover_image_url" :src="resolveApiAssetUrl(item.cover_image_url) || undefined" :alt="item.product_name" /><div v-else><span>暂无图片</span><small>点击进入上传商品图</small></div><em :class="`status-${item.status}`">{{ statusLabel(item.status) }}</em><b>编辑商品</b></div>
          <div class="merchant-product-card-body"><h2>{{ item.product_name }}</h2><p>{{ item.subtitle || `${item.category_name}${item.brand_name ? ` · ${item.brand_name}` : ''}` }}</p><div class="merchant-product-price"><strong>¥{{ item.min_price }}</strong><span v-if="item.min_price !== item.max_price">起</span></div><dl><div><dt>款式</dt><dd>{{ item.sku_count }}</dd></div><div><dt>可售</dt><dd>{{ item.available_quantity }}</dd></div><div><dt>销量</dt><dd>{{ item.sales_count }}</dd></div></dl></div>
        </RouterLink>
      </div>
      <p v-if="!loading && !error && items.length === 0" class="merchant-first-product-tip">还没有商品。点击左上角的“新增商品”，发布你的第一件商品吧。</p>
      <nav v-if="route.query.cursor || nextCursor" class="pagination"><button type="button" class="secondary" :disabled="!route.query.cursor" @click="router.back()">上一页</button><button type="button" class="secondary" :disabled="!nextCursor" @click="next">下一页</button></nav>
    </PageState>
  </section>
</template>
