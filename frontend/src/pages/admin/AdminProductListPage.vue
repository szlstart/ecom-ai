<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { adminGet, adminQuery, requireAdminToken, type AdminProductSummary } from '@/api/admin-catalog'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const route = useRoute(); const router = useRouter(); const auth = useAdminAuthStore()
const items = ref<AdminProductSummary[]>([]); const nextCursor = ref<string | null>(null)
const loading = ref(true); const error = ref(''); const q = ref(''); const status = ref(''); const storeId = ref('')

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

<template><section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">商品与库存</p><h1>商品管理</h1><p class="muted">列表按当前数据范围过滤；编辑、审核和发布使用独立命令。</p></div><div class="actions"><RouterLink v-if="auth.has('products:create')" class="button-link" to="/admin/products/new">新建商品</RouterLink><RouterLink v-if="auth.has('products:create')" to="/admin/products/import">批量导入</RouterLink><RouterLink v-if="auth.has('inventories:read')" to="/admin/inventories">库存管理</RouterLink></div></header><form class="filter-bar" @submit.prevent="submit"><label>商品名称/ID<input v-model="q" maxlength="255" /></label><label>店铺公开 ID<input v-model="storeId" maxlength="40" placeholder="sto_…" /></label><label>状态<select v-model="status"><option value="">全部</option><option value="draft">草稿</option><option value="pending_review">待审核</option><option value="rejected">已驳回</option><option value="on_sale">在售</option><option value="off_shelf">已下架</option></select></label><button>查询</button></form><PageState :loading="loading" :error="error" :empty="!loading && !error && items.length === 0" empty-title="没有匹配商品" @retry="load"><div class="table-wrap"><table><thead><tr><th>商品</th><th>店铺</th><th>分类/品牌</th><th>价格</th><th>状态</th><th>更新时间</th><th>操作</th></tr></thead><tbody><tr v-for="item in items" :key="item.product_id"><td><strong>{{ item.product_name }}</strong><small>{{ item.product_id }}</small></td><td>{{ item.store_name }}<small>{{ item.store_id }}</small></td><td>{{ item.category_name }} / {{ item.brand_name || '无品牌' }}</td><td>¥{{ item.min_price }}<span v-if="item.min_price !== item.max_price">–{{ item.max_price }}</span></td><td><span class="badge">{{ item.status }}</span></td><td>{{ item.updated_at }}</td><td><RouterLink :to="`/admin/products/${item.product_id}`">编辑/审核</RouterLink></td></tr></tbody></table></div><nav class="pagination"><button type="button" class="secondary" :disabled="!route.query.cursor" @click="router.back()">返回上一页</button><button type="button" class="secondary" :disabled="!nextCursor" @click="next">下一页</button></nav></PageState></section></template>
