<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import {
  adminCreate,
  adminGet,
  requireAdminToken,
  type AdminBatchJob,
  type ProductImportTemplate,
} from '@/api/admin-catalog'
import { downloadApiResource, errorMessage } from '@/api/http'
import AdminFileUpload from '@/components/AdminFileUpload.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const router = useRouter()
const storeId = ref('')
const fileId = ref('')
const template = ref<ProductImportTemplate | null>(null)
const busy = ref(false)
const error = ref('')

function token() { return requireAdminToken(auth.accessToken) }
async function loadTemplate() {
  try { template.value = (await adminGet<ProductImportTemplate>('/admin/product-import-template', token())).data }
  catch (cause) { error.value = errorMessage(cause) }
}
function uploaded(value: string) { fileId.value = value; error.value = '' }
async function downloadTemplate() {
  if (!template.value) return
  try {
    await downloadApiResource(
      '/admin/product-import-template.csv',
      token(),
      `${template.value.schema_version}.csv`,
    )
  } catch (cause) { error.value = errorMessage(cause) }
}
async function createJob() {
  if (!storeId.value.trim() || !fileId.value) {
    error.value = '请先填写店铺公开 ID，并完成导入文件上传和安全扫描。'
    return
  }
  busy.value = true; error.value = ''
  try {
    const job = (await adminCreate<AdminBatchJob>('/admin/batch-jobs', {
      job_type: 'product_import',
      store_id: storeId.value.trim(),
      input_file_id: fileId.value,
      schema_version: template.value?.schema_version || 'product-import-v1',
    }, token(), 'product-import')).data
    await router.push(`/admin/system/jobs/${job.job_id}`)
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}
onMounted(loadTemplate)
</script>

<template>
  <section class="admin-page-stack">
    <header class="page-heading"><div><p class="eyebrow">商品与库存</p><h1>商品批量导入</h1><p class="muted">文件扫描通过后进入异步预检；确认前不会创建商品或修改库存。</p></div><div class="actions"><RouterLink to="/admin/products">返回商品列表</RouterLink><RouterLink v-if="auth.has('jobs:read')" to="/admin/system/jobs">任务中心</RouterLink></div></header>
    <div v-if="error" class="notice error" role="alert">{{ error }}</div>
    <article class="admin-editor-card"><h2>1. 下载版本化模板</h2><p>当前 Schema：<code>{{ template?.schema_version || '加载中…' }}</code>，最多 {{ template?.maximum_rows || '—' }} 行，支持 {{ template?.supported_file_types.join(' / ') || '—' }}。</p><button type="button" class="secondary" :disabled="!template" @click="downloadTemplate">下载 CSV 模板</button><details v-if="template"><summary>查看字段说明</summary><div class="table-wrap"><table><thead><tr><th>字段</th><th>必填</th><th>说明</th><th>示例</th></tr></thead><tbody><tr v-for="column in template.columns" :key="column.name"><td><code>{{ column.name }}</code></td><td>{{ column.required ? '是' : '否' }}</td><td>{{ column.description }}</td><td>{{ column.example }}</td></tr></tbody></table></div></details></article>
    <article class="admin-editor-card"><h2>2. 选择店铺并上传</h2><label>店铺公开 ID<input v-model="storeId" maxlength="40" placeholder="sto_…" :disabled="Boolean(fileId)" /></label><p class="muted">店铺在上传会话创建时即绑定，上传后如需更换店铺，请重新上传文件。</p><AdminFileUpload purpose="admin_import" :business-context-id="storeId.trim() || null" label="CSV / XLSX 文件" @uploaded="uploaded" /><p v-if="fileId" class="success-text">已就绪文件：<code>{{ fileId }}</code></p></article>
    <article class="admin-editor-card"><h2>3. 创建预检任务</h2><p>Worker 会重新读取安全文件，逐行校验模板、类目、品牌、SKU 唯一性、价格和初始库存；错误行不会执行。</p><button type="button" :disabled="busy || !fileId || !storeId.trim()" @click="createJob">{{ busy ? '正在创建…' : '开始异步预检' }}</button></article>
  </section>
</template>
