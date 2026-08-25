<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  cancelAdminAgentRun,
  getAdminAgentRun,
  type AdminAgentRun,
} from '@/api/admin-observability'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const route = useRoute()
const runId = String(route.params.runId)
const item = ref<AdminAgentRun | null>(null)
const etag = ref('')
const reason = ref('')
const loading = ref(true)
const submitting = ref(false)
const error = ref('')
const notice = ref('')

function token() {
  if (!auth.accessToken) throw new Error('管理会话不可用')
  return auth.accessToken
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const result = await getAdminAgentRun(runId, token())
    item.value = result.data
    etag.value = result.headers.get('etag') ?? ''
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

async function cancelRun() {
  if (!item.value || reason.value.trim().length < 3) return
  if (!window.confirm('仅未执行或等待确认的 Run 可取消。确认提交取消命令吗？')) return
  submitting.value = true
  error.value = ''
  try {
    const result = await cancelAdminAgentRun(runId, reason.value.trim(), etag.value, token())
    item.value = result.data
    etag.value = result.headers.get('etag') ?? ''
    reason.value = ''
    notice.value = 'Run 已安全取消；尚未执行的确认和草稿已失效。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    submitting.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="admin-page-stack">
    <header class="page-heading">
      <div><p class="eyebrow">Redacted Agent Trace</p><h1>Agent Run 详情</h1><p class="muted">{{ runId }}</p></div>
      <RouterLink class="button-link secondary" to="/admin/observability">返回可观测性</RouterLink>
    </header>
    <p v-if="notice" class="alert success">{{ notice }}</p>
    <PageState :loading="loading" :error="error" :empty="!loading && !item" empty-title="Run 不存在" @retry="load">
      <article v-if="item" class="card wide-editor">
        <dl class="detail-list">
          <dt>状态</dt><dd><span class="badge">{{ item.status }}</span></dd>
          <dt>当前阶段</dt><dd>{{ item.current_phase }}</dd>
          <dt>Agent</dt><dd>{{ item.agent_code }} / v{{ item.agent_version_no }}</dd>
          <dt>会话类型</dt><dd>{{ item.conversation_type }}</dd>
          <dt>Trace ID</dt><dd>{{ item.trace_id }}</dd>
          <dt>上下文引用数</dt><dd>{{ item.context_ref_count }}</dd>
          <dt>错误码</dt><dd>{{ item.error_code ?? '-' }}</dd>
          <dt>降级原因</dt><dd>{{ item.degraded_reason ?? '-' }}</dd>
          <dt>更新时间</dt><dd>{{ new Date(item.updated_at).toLocaleString() }}</dd>
        </dl>
        <p class="muted">页面不返回原始 Prompt、消息正文、工具参数、用户标识或隐藏推理；业务排查通过授权后的资源页面进行。</p>
      </article>
      <form v-if="item && auth.has('ai_runtime:kill') && item.available_actions.includes('cancel')" class="card admin-editor wide-editor" @submit.prevent="cancelRun">
        <h2>取消未执行 Run</h2>
        <label>取消原因<textarea v-model="reason" required minlength="3" maxlength="500"></textarea></label>
        <p class="alert warning">运行中的模型或业务事务不会被强杀；若状态已经变化，服务端将拒绝旧版本命令。</p>
        <button class="danger" :disabled="submitting">{{ submitting ? '提交中…' : '取消 Run' }}</button>
      </form>
    </PageState>
  </section>
</template>
