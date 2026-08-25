<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  createToolVersion,
  getTool,
  publishToolVersion,
  rollbackToolVersion,
  type ToolSummary,
  type ToolVersionSummary,
} from '@/api/admin-ai'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const route = useRoute()
const toolCode = String(route.params.toolId)
const item = ref<ToolSummary | null>(null)
const loading = ref(true)
const error = ref('')
const notice = ref('')
const draft = reactive({
  input_schema: '{\n  "type": "object",\n  "additionalProperties": false\n}',
  output_schema: '{\n  "type": "object",\n  "additionalProperties": false\n}',
  evaluation_report: '{\n  "passed": false\n}',
})

function token() {
  if (!auth.accessToken) throw new Error('管理会话不可用')
  return auth.accessToken
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    item.value = (await getTool(toolCode, token())).data
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

async function addVersion() {
  error.value = ''
  notice.value = ''
  try {
    item.value = (await createToolVersion(toolCode, {
      input_schema: JSON.parse(draft.input_schema),
      output_schema: JSON.parse(draft.output_schema),
      evaluation_report: JSON.parse(draft.evaluation_report),
    }, token())).data
    notice.value = '不可变 Tool Version 草稿已创建。'
  } catch (cause) {
    error.value = cause instanceof SyntaxError
      ? 'Schema 或评估报告不是有效 JSON。'
      : errorMessage(cause)
  }
}

async function publish(version: ToolVersionSummary) {
  if (!window.confirm(`确认发起 v${version.version_no} 的双人发布审批？`)) return
  error.value = ''
  try {
    const approval = (await publishToolVersion(toolCode, version.version_no, token())).data
    notice.value = `审批 ${approval.approval_request_id} 已创建，需 ${approval.required_approval_count} 名独立管理员批准。`
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

async function rollback(version: ToolVersionSummary) {
  if (!window.confirm(`确认申请从当前版本回滚到历史 v${version.version_no}？该操作需要双人审批。`)) return
  error.value = ''
  try {
    const approval = (await rollbackToolVersion(toolCode, version.version_no, token())).data
    notice.value = `回滚审批 ${approval.approval_request_id} 已创建，审批执行前仍会校验版本漂移。`
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

onMounted(load)
</script>

<template>
  <section class="admin-page-stack">
    <header class="page-heading">
      <div><p class="eyebrow">MCP Tool Version</p><h1>{{ toolCode }}</h1><p class="muted">已发布版本不可原地编辑；发布与回滚均进入双人审批。</p></div>
      <RouterLink class="button-link secondary" to="/admin/ai/tools">返回 Tool 列表</RouterLink>
    </header>
    <p v-if="error" class="alert error">{{ error }}</p>
    <p v-if="notice" class="alert success">{{ notice }}</p>
    <PageState :loading="loading" :error="''" :empty="!loading && !item" empty-title="Tool 不存在" @retry="load">
      <div v-if="item" class="admin-split">
        <div class="card-list">
          <article class="card"><div class="card-heading"><div><h2>Definition</h2><p>{{ item.server_code }} · {{ item.risk_level }}</p></div><span class="badge">{{ item.status }}</span></div><p>当前发布 v{{ item.published_version ?? '-' }}</p></article>
          <article v-for="version in item.versions" :key="version.version_no" class="card">
            <div class="card-heading"><div><h2>Version {{ version.version_no }}</h2><p>评估：{{ version.evaluation_report.passed === true ? '通过' : '未通过' }}</p></div><span class="badge">{{ version.status }}</span></div>
            <details><summary>查看不可变 Schema</summary><pre>{{ JSON.stringify({ input: version.input_schema, output: version.output_schema }, null, 2) }}</pre></details>
            <div class="actions">
              <button v-if="auth.has('ai_tools:publish') && version.status === 'draft'" @click="publish(version)">申请发布</button>
              <button v-if="auth.has('ai_tools:publish') && version.status === 'retired' && version.evaluation_report.passed === true" class="danger" @click="rollback(version)">申请回滚到此版本</button>
            </div>
          </article>
        </div>
        <form v-if="auth.has('ai_tools:manage')" class="card admin-editor sticky-editor" @submit.prevent="addVersion">
          <h2>新建不可变版本</h2>
          <label>Input Schema<textarea v-model="draft.input_schema" required></textarea></label>
          <label>Output Schema<textarea v-model="draft.output_schema" required></textarea></label>
          <label>评估报告 JSON<textarea v-model="draft.evaluation_report" required></textarea></label>
          <p class="muted">评估报告未通过的草稿不能发起发布或回滚。</p>
          <button>创建草稿版本</button>
        </form>
      </div>
    </PageState>
  </section>
</template>
