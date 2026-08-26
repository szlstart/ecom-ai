<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { adminGet, adminQuery, requireAdminToken, type AdminProductSummary } from '@/api/admin-catalog'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const route = useRoute()
const router = useRouter()
const items = ref<AdminProductSummary[]>([])
const nextCursor = ref<string | null>(null)
const loading = ref(true)
const error = ref('')
const q = ref('')
const status = ref('')
const statusOptions = [
  ['', '全部状态'], ['draft', '草稿'], ['pending_review', '审核中'], ['rejected', '需修改'], ['on_sale', '销售中'], ['off_shelf', '已下架'],
] as const

function one(value: unknown) { return typeof value === 'string' ? value : '' }
function statusLabel(value: string) { return ({ draft: '草稿', pending_review: '审核中', rejected: '需修改', on_sale: '销售中', off_shelf: '已下架' } as Record<string, string>)[value] ?? value }
function sync() { q.value = one(route.query.q); status.value = one(route.query.status) }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await adminGet<{ items: AdminProductSummary[]; next_cursor: string | null }>(
      `/admin/products${adminQuery({ q: one(route.query.q), status: one(route.query.status), cursor: one(route.query.cursor), limit: 50 })}`,
      requireAdminToken(auth.accessToken),
    )
    items.value = response.data.items
    nextCursor.value = response.data.next_cursor
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

function search() {
  void router.push({ path: '/merchant/products', query: Object.fromEntries(Object.entries({ q: q.value.trim(), status: status.value }).filter(([, value]) => value)) })
}
function next() { if (nextCursor.value) void router.push({ path: '/merchant/products', query: { ...route.query, cursor: nextCursor.value } }) }

onMounted(() => { sync(); void load() })
watch(() => route.fullPath, () => { sync(); void load() })
</script>

<template>
  <section class="merchant-page-stack">
    <header class="merchant-page-heading"><div><p class="eyebrow">商品中心</p><h1>商品管理</h1><p>新建、完善、提交审核、上架或下架你的商品。</p></div><RouterLink class="button-link" to="/merchant/products/new">发布新商品</RouterLink></header>
    <form class="merchant-filter-bar" @submit.prevent="search"><label>搜索商品<input v-model="q" maxlength="255" placeholder="输入商品名称" /></label><label>商品状态<select v-model="status"><option v-for="entry in statusOptions" :key="entry[0] || 'all'" :value="entry[0]">{{ entry[1] }}</option></select></label><button>查询</button></form>
    <PageState :loading="loading" :error="error" :empty="!loading && !error && items.length === 0" empty-title="当前没有符合条件的商品" @retry="load">
      <div class="merchant-product-table"><div class="merchant-product-row merchant-product-head"><span>商品</span><span>售价</span><span>状态</span><span>更新时间</span><span>操作</span></div><div v-for="item in items" :key="item.product_id" class="merchant-product-row"><span><strong>{{ item.product_name }}</strong><small>{{ item.category_name }}{{ item.brand_name ? ` · ${item.brand_name}` : '' }}</small></span><span><strong>¥{{ item.min_price }}</strong><small v-if="item.min_price !== item.max_price">至 ¥{{ item.max_price }}</small></span><span><span class="badge">{{ statusLabel(item.status) }}</span></span><span>{{ new Date(item.updated_at).toLocaleString('zh-CN') }}</span><span><RouterLink :to="`/merchant/products/${item.product_id}`">编辑商品</RouterLink></span></div></div>
      <nav class="pagination"><button type="button" class="secondary" :disabled="!route.query.cursor" @click="router.back()">上一页</button><button type="button" class="secondary" :disabled="!nextCursor" @click="next">下一页</button></nav>
    </PageState>
  </section>
</template>
