<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { adminGet, adminQuery, requireAdminToken, type AdminBatchJob } from '@/api/admin-catalog'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const route = useRoute()
const knowledgeMode = route.path.startsWith('/admin/knowledge/')
const jobType = knowledgeMode ? 'knowledge_index' : 'product_import'
const jobs = ref<AdminBatchJob[]>([])
const loading = ref(true)
const error = ref('')
const status = ref('')
const nextCursor = ref<string | null>(null)
const cursor = ref<string | null>(null)
async function load(reset = false) {
  if (reset) cursor.value = null
  loading.value = true; error.value = ''
  try {
    const response = await adminGet<{ items: AdminBatchJob[]; next_cursor: string | null }>(
      `/admin/batch-jobs${adminQuery({ job_type: jobType, status: status.value, cursor: cursor.value, limit: 50 })}`,
      requireAdminToken(auth.accessToken),
    )
    jobs.value = response.data.items; nextCursor.value = response.data.next_cursor
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}
function next() { if (nextCursor.value) { cursor.value = nextCursor.value; void load() } }
onMounted(() => load())
</script>

<template><section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">系统任务</p><h1>{{ knowledgeMode ? '知识索引任务' : '批处理任务中心' }}</h1><p class="muted">任务状态来自 MySQL 权威资源；执行状态由 PostgreSQL 子任务单调映射。</p></div><RouterLink v-if="!knowledgeMode && auth.has('products:create')" class="button-link" to="/admin/products/import">新建商品导入</RouterLink></header><form class="filter-bar" @submit.prevent="load(true)"><label>状态<select v-model="status"><option value="">全部</option><option v-for="value in ['created','validating','awaiting_confirmation','queued','running','succeeded','partial','failed','cancelled','expired']" :key="value" :value="value">{{ value }}</option></select></label><button>查询</button></form><PageState :loading="loading" :error="error" :empty="!loading && !error && jobs.length === 0" empty-title="暂无批处理任务" @retry="load()"><div class="table-wrap"><table><thead><tr><th>任务</th><th>店铺</th><th>状态</th><th>总数</th><th>成功/有效</th><th>失败/无效</th><th>创建时间</th><th>操作</th></tr></thead><tbody><tr v-for="job in jobs" :key="job.job_id"><td><strong>{{ knowledgeMode ? '知识索引' : '商品导入' }}</strong><small>{{ job.job_id }}</small></td><td>{{ job.store_id }}</td><td><span class="badge">{{ job.status }}</span></td><td>{{ job.total_count }}</td><td>{{ job.success_count }}</td><td>{{ job.failure_count }}</td><td>{{ job.requested_at }}</td><td><RouterLink :to="knowledgeMode ? `/admin/knowledge/indexing-jobs/${job.job_id}` : `/admin/system/jobs/${job.job_id}`">查看详情</RouterLink></td></tr></tbody></table></div><nav class="pagination"><button type="button" class="secondary" :disabled="!cursor" @click="cursor = null; load()">返回首批</button><button type="button" class="secondary" :disabled="!nextCursor" @click="next">下一批</button></nav></PageState></section></template>
