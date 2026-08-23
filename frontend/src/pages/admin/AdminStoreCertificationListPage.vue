<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { adminGet, adminQuery, requireAdminToken, type AdminCertificationSummary } from '@/api/admin-catalog'
import { errorMessage, type PaginationMeta } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const items = ref<AdminCertificationSummary[]>([])
const reviewStatus = ref('pending')
const loading = ref(true)
const error = ref('')
const pagination = ref<PaginationMeta | null>(null)

async function load(cursor = '') {
  loading.value = true; error.value = ''
  try {
    const response = await adminGet<{ items: AdminCertificationSummary[]; next_cursor: string | null }>(`/admin/store-certifications${adminQuery({ review_status: reviewStatus.value, cursor, limit: 50 })}`, requireAdminToken(auth.accessToken))
    items.value = response.data.items; pagination.value = response.meta.pagination
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

onMounted(() => load())
</script>

<template><section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">店铺管理</p><h1>认证审核队列</h1><p class="muted">审核决定和材料补交均形成不可覆盖的版本事件。</p></div><RouterLink to="/admin/stores">返回店铺列表</RouterLink></header><form class="filter-bar" @submit.prevent="load()"><label>审核状态<select v-model="reviewStatus"><option value="">全部</option><option value="pending">待审核</option><option value="more_info_required">待补件</option><option value="approved">已通过</option><option value="rejected">已拒绝</option></select></label><button>查询</button></form><PageState :loading="loading" :error="error" :empty="!loading && !error && items.length === 0" empty-title="当前队列为空" @retry="load()"><div class="table-wrap"><table><thead><tr><th>店铺</th><th>认证类型</th><th>状态</th><th>材料版本</th><th>有效期</th><th>操作</th></tr></thead><tbody><tr v-for="item in items" :key="item.certification_id"><td><strong>{{ item.store_name }}</strong><small>{{ item.store_id }}</small></td><td>{{ item.certification_type }}</td><td><span class="badge">{{ item.review_status }}</span></td><td>v{{ item.material_version }}</td><td>{{ item.valid_from || '—' }} 至 {{ item.valid_until || '—' }}</td><td><RouterLink :to="`/admin/store-certifications/${item.certification_id}`">审核详情</RouterLink></td></tr></tbody></table></div><nav v-if="pagination" class="pagination"><button type="button" class="secondary" :disabled="!pagination.has_previous" @click="load(pagination.previous_cursor || '')">上一页</button><button type="button" class="secondary" :disabled="!pagination.has_next" @click="load(pagination.next_cursor || '')">下一页</button></nav></PageState></section></template>
