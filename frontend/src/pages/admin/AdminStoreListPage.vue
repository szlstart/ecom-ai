<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { adminGet, adminQuery, requireAdminToken, type AdminStore } from '@/api/admin-catalog'
import { errorMessage, type PaginationMeta } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const items = ref<AdminStore[]>([])
const loading = ref(true)
const error = ref('')
const q = ref('')
const status = ref('')
const cursor = ref('')
const pagination = ref<PaginationMeta | null>(null)

async function load(nextCursor = '') {
  loading.value = true; error.value = ''
  try {
    const response = await adminGet<{ items: AdminStore[]; next_cursor: string | null }>(`/admin/stores${adminQuery({ q: q.value.trim(), status: status.value, cursor: nextCursor, limit: 50 })}`, requireAdminToken(auth.accessToken))
    items.value = response.data.items; cursor.value = nextCursor; pagination.value = response.meta.pagination
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

onMounted(() => load())
</script>

<template>
  <section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">店铺管理</p><h1>店铺运营</h1><p class="muted">列表只返回当前管理员数据范围内的店铺。</p></div><RouterLink v-if="auth.has('stores:review')" class="button-link secondary" to="/admin/store-certifications">认证审核队列</RouterLink></header><form class="filter-bar" @submit.prevent="load()"><label>店铺名称<input v-model="q" maxlength="128" /></label><label>状态<select v-model="status"><option value="">全部</option><option value="pending">待开通</option><option value="active">营业中</option><option value="suspended">已暂停</option><option value="closed">已关闭</option></select></label><button>查询</button></form><PageState :loading="loading" :error="error" :empty="!loading && !error && items.length === 0" empty-title="没有匹配的店铺" @retry="load()"><div class="table-wrap"><table><thead><tr><th>店铺</th><th>状态</th><th>评分</th><th>关注/销量</th><th>开店时间</th><th>操作</th></tr></thead><tbody><tr v-for="item in items" :key="item.store_id"><td><strong>{{ item.store_name }}</strong><small>{{ item.store_id }} · Owner {{ item.owner_user_id }}</small></td><td><span class="badge">{{ item.status }}</span></td><td>{{ item.rating_score }}（{{ item.rating_count }}）</td><td>{{ item.follower_count }} / {{ item.sales_count }}</td><td>{{ item.opened_at || '—' }}</td><td><RouterLink :to="`/admin/stores/${item.store_id}`">进入运营</RouterLink></td></tr></tbody></table></div><nav v-if="pagination" class="pagination"><button type="button" class="secondary" :disabled="!pagination.has_previous" @click="load(pagination.previous_cursor || '')">上一页</button><span>每页 {{ pagination.limit }} 条</span><button type="button" class="secondary" :disabled="!pagination.has_next" @click="load(pagination.next_cursor || '')">下一页</button></nav></PageState></section>
</template>
