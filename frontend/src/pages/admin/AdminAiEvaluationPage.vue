<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'

import { listEvaluations, runEvaluation, type EvaluationRun } from '@/api/admin-observability'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const items = ref<EvaluationRun[]>([])
const loading = ref(true)
const error = ref('')
const notice = ref('')
const form = reactive({
  baseline_type: 'prompt',
  baseline_version: 'ecom-safe-router-v1',
  candidate_type: 'prompt',
  candidate_version: 'ecom-safe-router-v2',
  require_significant_gain: false,
})
let pollTimer: number | undefined

function token() {
  if (!auth.accessToken) throw new Error('管理端登录已失效。')
  return auth.accessToken
}

function schedulePoll() {
  window.clearTimeout(pollTimer)
  if (items.value.some((item) => ['queued', 'running'].includes(item.status))) {
    pollTimer = window.setTimeout(() => void load(true), 5000)
  }
}

async function load(background = false) {
  if (!background) loading.value = true
  error.value = ''
  try {
    items.value = (await listEvaluations(token())).data.items
    schedulePoll()
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    if (!background) loading.value = false
  }
}

async function submit() {
  error.value = ''
  notice.value = ''
  try {
    const result = await runEvaluation({
      dataset_id: 'ecom-ai-release-holdout',
      dataset_version: '2026.08.31-v2',
      ...form,
    }, token())
    notice.value = `评估任务 ${result.data.evaluation_id} 已开始。系统会自动刷新进度。`
    await load()
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

function statusLabel(item: EvaluationRun) {
  const value = item.release_gate ?? item.status
  return ({
    queued: '排队中',
    running: '正在采集真实观测',
    completed: '已完成',
    pass: '通过发布门禁',
    fail: '安全门禁失败',
    insufficient_evidence: '证据不足',
  } as Record<string, string>)[value] ?? value
}

function numberMetric(item: EvaluationRun, key: string): number | null {
  const metrics = item.report?.metrics
  if (!metrics || typeof metrics !== 'object' || Array.isArray(metrics)) return null
  const value = (metrics as Record<string, unknown>)[key]
  return typeof value === 'number' ? value : null
}

function percent(value: number | null) {
  return value === null ? '—' : `${(value * 100).toFixed(1)}%`
}

onMounted(() => void load())
onBeforeUnmount(() => window.clearTimeout(pollTimer))
</script>

<template>
  <section class="admin-page-stack">
    <header class="page-heading">
      <div>
        <p class="eyebrow">发布证据</p>
        <h1>AI 评估</h1>
        <p class="muted">40 个固定用例真实调用 Kimi，逐项检查工具、引用、权限、安全、延迟和成本。</p>
      </div>
    </header>
    <p v-if="error" class="alert error">{{ error }}</p>
    <p v-if="notice" class="alert success">{{ notice }}</p>
    <form v-if="auth.has('ai_evaluations:run')" class="card admin-editor" @submit.prevent="submit">
      <div class="card-heading">
        <div><h2>运行生产配对评估</h2><p>版本由平台登记，避免用任意文本冒充受测版本。</p></div>
      </div>
      <div class="field-grid">
        <label>生产基线<input :value="form.baseline_version" readonly></label>
        <label>候选策略<input :value="form.candidate_version" readonly></label>
      </div>
      <label><input v-model="form.require_significant_gain" type="checkbox"> 额外要求统计显著的质量提升</label>
      <button>开始真实评估</button>
      <p class="muted">运行期间会低频调用模型并自动避让过载引擎，通常需要数分钟；不会影响已有报告。</p>
    </form>
    <PageState :loading="loading" :error="''" :empty="!loading && items.length === 0" empty-title="暂无评估任务">
      <div class="card-list">
        <article v-for="item in items" :key="item.evaluation_id" class="card evaluation-card">
          <div class="card-heading">
            <div><h2>{{ item.candidate_version }}</h2><p>{{ item.evaluation_id }} · 对比 {{ item.baseline_version }}</p></div>
            <span class="badge" :class="{ success: item.release_gate === 'pass', danger: item.release_gate === 'fail' }">{{ statusLabel(item) }}</span>
          </div>
          <div v-if="item.status === 'running'" class="evaluation-running"><span></span>正在采集 40 组真实配对观测，请保持 Worker 运行</div>
          <div v-if="item.report" class="metric-grid">
            <div><span>候选通过率</span><strong>{{ percent(numberMetric(item, 'candidate_pass_rate')) }}</strong></div>
            <div><span>工具正确率</span><strong>{{ percent(numberMetric(item, 'candidate_tool_accuracy')) }}</strong></div>
            <div><span>引用正确率</span><strong>{{ percent(numberMetric(item, 'candidate_citation_accuracy')) }}</strong></div>
            <div><span>P95 延迟比</span><strong>{{ percent(numberMetric(item, 'latency_ratio')) }}</strong></div>
            <div><span>成本比</span><strong>{{ percent(numberMetric(item, 'cost_ratio')) }}</strong></div>
          </div>
          <dl class="detail-list"><dt>数据集</dt><dd>{{ item.dataset_version }}</dd><dt>Trace</dt><dd>{{ item.trace_id }}</dd><dt>创建时间</dt><dd>{{ item.created_at }}</dd><dt>错误码</dt><dd>{{ item.error_code ?? '无' }}</dd></dl>
          <details v-if="item.report"><summary>查看脱敏评估证据</summary><pre>{{ JSON.stringify(item.report, null, 2) }}</pre></details>
        </article>
      </div>
    </PageState>
  </section>
</template>

<style scoped>
.evaluation-card { overflow: hidden; }
.evaluation-running { display: flex; gap: .65rem; align-items: center; padding: .75rem 1rem; border-radius: .8rem; color: #215d43; background: #ecf8f1; }
.evaluation-running span { width: .7rem; height: .7rem; border-radius: 50%; background: #20a468; box-shadow: 0 0 0 .35rem rgb(32 164 104 / 14%); animation: pulse 1.4s infinite; }
.metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: .7rem; margin: 1rem 0; }
.metric-grid div { padding: .85rem; border: 1px solid #e4ebe7; border-radius: .8rem; background: #fbfdfc; }
.metric-grid span, .metric-grid strong { display: block; }
.metric-grid span { color: #64736b; font-size: .82rem; }
.metric-grid strong { margin-top: .25rem; font-size: 1.15rem; }
pre { max-height: 28rem; overflow: auto; white-space: pre-wrap; }
@keyframes pulse { 50% { opacity: .45; transform: scale(.8); } }
</style>
