<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import {
  adminCommand,
  adminGet,
  adminQuery,
  requireAdminToken,
  type AdminBatchJob,
  type AdminBatchJobItem,
} from '@/api/admin-catalog'
import { downloadApiResource, errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const route = useRoute()
const jobId = String(route.params.jobId)
const job = ref<AdminBatchJob | null>(null)
const items = ref<AdminBatchJobItem[]>([])
const itemStatus = ref('')
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const notice = ref('')
let timer: number | null = null
function token() { return requireAdminToken(auth.accessToken) }
function active(status: string) { return ['created', 'validating', 'queued', 'running'].includes(status) }
async function load() {
  try {
    job.value = (await adminGet<AdminBatchJob>(`/admin/batch-jobs/${encodeURIComponent(jobId)}`, token())).data
    const result = await adminGet<{ items: AdminBatchJobItem[]; next_cursor: string | null }>(
      `/admin/batch-jobs/${encodeURIComponent(jobId)}/items${adminQuery({ status: itemStatus.value, limit: 100 })}`,
      token(),
    )
    items.value = result.data.items; error.value = ''
    if (job.value && active(job.value.status)) schedule()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}
function schedule() {
  if (timer !== null) window.clearTimeout(timer)
  timer = window.setTimeout(() => { timer = null; void load() }, 2000)
}
async function confirm() {
  if (!job.value?.preview_hash) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    job.value = (await adminCommand<AdminBatchJob>(
      `/admin/batch-jobs/${encodeURIComponent(jobId)}/confirmations`,
      { preview_hash: job.value.preview_hash }, token(), job.value.version, 'batch-confirm',
    )).data
    notice.value = '已确认，任务进入执行队列。'; schedule()
  } catch (cause) { error.value = errorMessage(cause); await load() }
  finally { busy.value = false }
}
async function cancel() {
  if (!job.value) return
  const reason = window.prompt('请输入取消原因（至少 2 个字符）')?.trim()
  if (!reason || reason.length < 2) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    job.value = (await adminCommand<AdminBatchJob>(
      `/admin/batch-jobs/${encodeURIComponent(jobId)}/cancellations`,
      { reason }, token(), job.value.version, 'batch-cancel',
    )).data
    notice.value = job.value.status === 'running' ? '已记录协作取消请求。' : '任务已取消。'
  } catch (cause) { error.value = errorMessage(cause); await load() }
  finally { busy.value = false }
}
async function downloadErrorReport() {
  if (!job.value?.error_file_id) return
  try {
    await downloadApiResource(
      `/files/${encodeURIComponent(job.value.error_file_id)}`,
      token(),
      `${jobId}-errors.csv`,
    )
  } catch (cause) { error.value = errorMessage(cause) }
}
onMounted(load)
onBeforeUnmount(() => { if (timer !== null) window.clearTimeout(timer) })
</script>

<template><section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">批处理任务</p><h1>商品导入任务</h1><p class="muted"><code>{{ jobId }}</code></p></div><div class="actions"><RouterLink to="/admin/system/jobs">返回任务中心</RouterLink><RouterLink to="/admin/products">商品列表</RouterLink></div></header><div v-if="notice" class="notice success" aria-live="polite">{{ notice }}</div><div v-if="error" class="notice error" role="alert">{{ error }}</div><PageState :loading="loading" :error="''" :empty="!job" empty-title="任务不存在" @retry="load"><template v-if="job"><article class="admin-editor-card"><div class="job-summary"><div><small>状态</small><strong class="badge">{{ job.status }}</strong></div><div><small>总行数</small><strong>{{ job.total_count }}</strong></div><div><small>有效/成功</small><strong>{{ job.success_count }}</strong></div><div><small>无效/失败</small><strong>{{ job.failure_count }}</strong></div></div><dl class="detail-grid"><div><dt>店铺</dt><dd>{{ job.store_id }}</dd></div><div><dt>模板版本</dt><dd>{{ job.schema_version }}</dd></div><div><dt>预览 Hash</dt><dd><code>{{ job.preview_hash || '预检中' }}</code></dd></div><div><dt>过期时间</dt><dd>{{ job.expires_at || '—' }}</dd></div></dl><p v-if="job.error_summary" class="error-text">{{ job.error_code }}：{{ job.error_summary }}</p><div class="actions"><button v-if="job.available_actions.includes('confirm') && auth.has('products:create')" :disabled="busy" @click="confirm">确认执行有效批次</button><button v-if="job.available_actions.includes('cancel') && auth.has('products:create')" class="danger" :disabled="busy" @click="cancel">取消任务</button><button v-if="job.error_file_id && auth.has('products:create')" class="secondary" :disabled="busy" @click="downloadErrorReport">下载错误报告</button><button class="secondary" :disabled="busy" @click="load">刷新</button></div></article><article class="admin-editor-card"><div class="section-heading"><div><h2>逐项结果</h2><p class="muted">行号仅用于定位原始文件，不回显完整敏感输入。</p></div><label>筛选<select v-model="itemStatus" @change="load"><option value="">全部</option><option v-for="value in ['valid','invalid','succeeded','failed','skipped']" :key="value" :value="value">{{ value }}</option></select></label></div><div class="table-wrap"><table><thead><tr><th>源文件行</th><th>状态</th><th>结果资源</th><th>错误码</th><th>说明</th></tr></thead><tbody><tr v-for="item in items" :key="item.item_key"><td>{{ item.item_key }}</td><td><span class="badge">{{ item.item_status }}</span></td><td><RouterLink v-if="item.resource_type === 'product' && item.resource_id" :to="`/admin/products/${item.resource_id}`">{{ item.resource_id }}</RouterLink><span v-else>—</span></td><td><code>{{ item.error_code || '—' }}</code></td><td>{{ item.error_message || '—' }}</td></tr><tr v-if="items.length === 0"><td colspan="5">当前没有逐项结果，预检可能仍在进行。</td></tr></tbody></table></div></article></template></PageState></section></template>
