<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { adminGet, adminQuery, requireAdminToken, type AdminProductSummary } from '@/api/admin-catalog'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const route = useRoute(); const router = useRouter(); const auth = useAdminAuthStore()
const items = ref<AdminProductSummary[]>([]); const nextCursor = ref<string | null>(null)
const loading = ref(true); const error = ref(''); const q = ref(''); const status = ref(''); const storeId = ref('')
const imageErrors = ref(new Set<string>())
const onSaleCount = computed(() => items.value.filter((item) => item.status === 'on_sale').length)
const reviewCount = computed(() => items.value.filter((item) => item.status === 'pending_review').length)
function statusLabel(value: string) { return ({ draft: '草稿', pending_review: '待审核', rejected: '需修改', on_sale: '销售中', off_shelf: '已下架' } as Record<string, string>)[value] ?? value }
function markImageError(productId: string) { imageErrors.value = new Set(imageErrors.value).add(productId) }

function one(value: unknown) { return typeof value === 'string' ? value : '' }
function sync() { q.value = one(route.query.q); status.value = one(route.query.status); storeId.value = one(route.query.store_id) }
async function load() {
  loading.value = true; error.value = ''
  try {
    const response = await adminGet<{ items: AdminProductSummary[]; next_cursor: string | null }>(`/admin/products${adminQuery({ q: one(route.query.q), status: one(route.query.status), store_id: one(route.query.store_id), cursor: one(route.query.cursor), limit: 50 })}`, requireAdminToken(auth.accessToken))
    items.value = response.data.items; nextCursor.value = response.data.next_cursor
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}
function submit() { void router.push({ path: '/admin/products', query: Object.fromEntries(Object.entries({ q: q.value.trim(), status: status.value, store_id: storeId.value.trim() }).filter(([, value]) => value)) }) }
function next() { if (nextCursor.value) void router.push({ path: '/admin/products', query: { ...route.query, cursor: nextCursor.value } }) }
onMounted(() => { sync(); void load() }); watch(() => route.fullPath, () => { sync(); void load() })
</script>

<template><section class="admin-page-stack admin-product-catalog-page">
  <header class="admin-entity-hero"><div><p class="eyebrow">商品治理</p><h1>全平台商品</h1><p>按店铺查看商品、审核发布状态与库存健康度，异常商品一眼可见。</p></div><div class="actions"><RouterLink v-if="auth.has('products:create')" class="button-link" to="/admin/stores">选择店铺后新增商品</RouterLink><RouterLink v-if="auth.has('products:create')" class="button-link secondary" to="/admin/products/import">批量导入</RouterLink></div></header>
  <div class="admin-entity-stats"><article><span class="blue">品</span><div><small>当前载入</small><strong>{{ items.length }}</strong></div></article><article><span class="green">售</span><div><small>销售中</small><strong>{{ onSaleCount }}</strong></div></article><article><span class="red">审</span><div><small>待审核</small><strong>{{ reviewCount }}</strong></div></article></div>
  <section class="admin-list-panel"><form class="admin-product-toolbar" @submit.prevent="submit"><label class="admin-inline-search"><span>⌕</span><input v-model="q" maxlength="255" placeholder="搜索商品名称或商品 ID" /></label><input v-model="storeId" maxlength="40" placeholder="店铺 ID（可选）" /><select v-model="status"><option value="">全部状态</option><option value="draft">草稿</option><option value="pending_review">待审核</option><option value="rejected">需修改</option><option value="on_sale">销售中</option><option value="off_shelf">已下架</option></select><button>搜索</button><RouterLink v-if="auth.has('inventories:read')" to="/admin/inventories">库存中心 →</RouterLink></form>
  <PageState :loading="loading" :error="error" :empty="!loading && !error && items.length === 0" empty-title="没有匹配商品" @retry="load"><div class="admin-product-grid"><RouterLink v-for="item in items" :key="item.product_id" class="admin-product-card" :to="`/admin/products/${item.product_id}`"><div class="admin-product-image"><img v-if="item.cover_image_url && !imageErrors.has(item.product_id)" :src="item.cover_image_url" alt="" @error="markImageError(item.product_id)" /><span v-else>▦<small>暂无可用图片</small></span><i :class="item.status">{{ statusLabel(item.status) }}</i></div><div class="admin-product-card-copy"><small>{{ item.store_name }}</small><h2>{{ item.product_name }}</h2><p><strong>¥{{ item.min_price }}</strong><span v-if="item.min_price !== item.max_price">起</span></p><dl><span>销量 {{ item.sales_count }}</span><span>库存 {{ item.available_quantity }}</span><span>{{ item.sku_count }} 款式</span></dl><footer><small>{{ item.product_id }}</small><b>查看与编辑 →</b></footer></div></RouterLink></div><nav class="pagination"><button type="button" class="secondary" :disabled="!route.query.cursor" @click="router.back()">返回上一页</button><button type="button" class="secondary" :disabled="!nextCursor" @click="next">下一页</button></nav></PageState></section>
</section></template>
