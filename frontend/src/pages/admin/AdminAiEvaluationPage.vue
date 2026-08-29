<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { listEvaluations, runEvaluation, type EvaluationRun } from '@/api/admin-observability'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const items = ref<EvaluationRun[]>([]), loading = ref(true), error = ref(''), notice = ref('')
const form = reactive({ baseline_type: 'agent', baseline_version: '', candidate_type: 'agent', candidate_version: '', require_significant_gain: false })
function token() { if (!auth.accessToken) throw new Error('管理端登录已失效。'); return auth.accessToken }
async function load() { loading.value = true; error.value = ''; try { items.value = (await listEvaluations(token())).data.items } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false } }
async function submit() { error.value = ''; notice.value = ''; try { const result = await runEvaluation({ dataset_id: 'ecom-ai-release-holdout', dataset_version: '2026.08.25-v1', ...form }, token()); notice.value = `评估任务 ${result.data.evaluation_id} 已进入队列。`; await load() } catch (cause) { error.value = errorMessage(cause) } }
onMounted(load)
</script>

<template><section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">发布证据</p><h1>AI 评估</h1><p class="muted">固定 Golden Dataset、候选与基线版本；安全 Holdout 失败不能被平均质量抵消。</p></div></header><p v-if="error" class="alert error">{{ error }}</p><p v-if="notice" class="alert success">{{ notice }}</p><form v-if="auth.has('ai_evaluations:run')" class="card admin-editor" @submit.prevent="submit"><h2>创建配对评估</h2><div class="field-grid"><label>基线类型<select v-model="form.baseline_type"><option>agent</option><option>skill</option><option>model</option><option>prompt</option><option>tool</option><option>multi_agent</option></select></label><label>基线版本<input v-model="form.baseline_version" required pattern="[A-Za-z0-9._:-]+"></label><label>候选类型<select v-model="form.candidate_type"><option>agent</option><option>skill</option><option>model</option><option>prompt</option><option>tool</option><option>multi_agent</option></select></label><label>候选版本<input v-model="form.candidate_version" required pattern="[A-Za-z0-9._:-]+"></label></div><label><input v-model="form.require_significant_gain" type="checkbox"> 要求预注册的显著质量收益（Multi-Agent）</label><button>加入评估队列</button></form><PageState :loading="loading" :error="''" :empty="!loading && items.length === 0" empty-title="暂无评估任务"><div class="card-list"><article v-for="item in items" :key="item.evaluation_id" class="card"><div class="card-heading"><div><h2>{{ item.candidate_type }} · {{ item.candidate_version }}</h2><p>{{ item.evaluation_id }} · 对比 {{ item.baseline_version }}</p></div><span class="badge">{{ item.release_gate ?? item.status }}</span></div><dl class="detail-list"><dt>数据集</dt><dd>{{ item.dataset_version }}</dd><dt>Trace</dt><dd>{{ item.trace_id }}</dd><dt>创建时间</dt><dd>{{ item.created_at }}</dd><dt>错误码</dt><dd>{{ item.error_code ?? '-' }}</dd></dl><details v-if="item.report"><summary>脱敏评估报告</summary><pre>{{ JSON.stringify(item.report, null, 2) }}</pre></details></article></div></PageState></section></template>
