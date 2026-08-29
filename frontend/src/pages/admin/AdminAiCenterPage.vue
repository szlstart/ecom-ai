<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { getModelProviderHealth, type ModelProviderHealth } from '@/api/admin-ai'
import { errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const provider = ref<ModelProviderHealth | null>(null)
const providerLoading = ref(false)
const providerError = ref('')
const modules = [
  { title: 'Agent', copy: '配置店铺客服与专属客服的模型、系统规则和版本。', icon: '◈', route: '/admin/ai/agents', permission: 'ai_agents:read', tone: 'blue' },
  { title: 'Skill', copy: '把业务能力组织成可测试、可发布、可回滚的技能。', icon: '⌁', route: '/admin/ai/skills', permission: 'ai_skills:read', tone: 'purple' },
  { title: 'MCP 工具', copy: '管理 Agent 可以调用的业务工具、版本与健康状态。', icon: '⌘', route: '/admin/ai/tools', permission: 'ai_tools:read', tone: 'green' },
  { title: 'RAG 知识库', copy: '维护平台和店铺知识，跟踪切片、索引与检索状态。', icon: '▱', route: '/admin/knowledge/documents', permission: 'knowledge:read', tone: 'orange' },
  { title: '权限与确认', copy: '定义数据范围、调用预算、敏感操作确认和硬阻断。', icon: '♢', route: '/admin/ai/policies', permission: 'ai_policies:read', tone: 'red' },
  { title: '评估与观测', copy: '检查正确率、越权阻断、延迟、成本和运行链路。', icon: '⌇', route: '/admin/ai/evaluations', permission: 'ai_evaluations:read', tone: 'cyan' },
]

const providerLabel = computed(() => ({
  available: '模型服务可用',
  degraded: '模型能力降级',
  unavailable: '模型服务不可用',
  unconfigured: '模型尚未配置',
})[provider.value?.status ?? 'unavailable'])

async function loadProvider(force = false) {
  if (!auth.accessToken || providerLoading.value || !auth.has('ai_observability:read')) return
  providerLoading.value = true
  providerError.value = ''
  try { provider.value = (await getModelProviderHealth(auth.accessToken, force)).data }
  catch (cause) { providerError.value = errorMessage(cause) }
  finally { providerLoading.value = false }
}

function checkedAt(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'medium' }).format(new Date(value))
}

onMounted(() => void loadProvider())
</script>

<template><section class="admin-page-stack admin-ai-center-page">
  <header class="admin-ai-center-hero"><div><p class="eyebrow">AI CONTROL PLANE</p><h1>AI 智能客服控制中心</h1><p>用业务语言管理 Agent、Skill、MCP 和 RAG。配置变更先评估、再审批、后发布，生产问题可追踪、可回滚。</p><div><span><i />运行保护已启用</span><RouterLink to="/admin/observability">查看实时运行状态 →</RouterLink></div></div><span class="admin-ai-hero-orb">✦</span></header>
  <section v-if="auth.has('ai_observability:read')" class="admin-provider-health admin-panel" :class="provider?.status || 'loading'" aria-live="polite">
    <header><div><p class="eyebrow">MODEL PROVIDER</p><h2>Kimi 模型服务</h2></div><button type="button" class="secondary" :disabled="providerLoading" @click="loadProvider(true)">{{ providerLoading ? '正在检测…' : '重新检测' }}</button></header>
    <p v-if="providerError" class="alert error">{{ providerError }}</p>
    <div v-else-if="provider" class="admin-provider-health-grid">
      <div class="provider-health-state"><span aria-hidden="true">{{ provider.status === 'available' ? '✓' : provider.status === 'degraded' ? '!' : '×' }}</span><div><strong>{{ providerLabel }}</strong><small>{{ provider.provider === 'moonshot' ? 'Moonshot' : 'OpenAI 兼容服务' }} · {{ provider.configured_model || '未选择模型' }}</small></div></div>
      <dl><div><dt>模型发现</dt><dd>{{ provider.model_available ? '通过' : '未通过' }}</dd></div><div><dt>结构化输出</dt><dd>{{ provider.structured_output ? '通过' : '未通过' }}</dd></div><div><dt>流式输出</dt><dd>{{ provider.streaming ? '通过' : '未通过' }}</dd></div><div><dt>用量统计</dt><dd>{{ provider.usage_reporting ? '通过' : '未通过' }}</dd></div></dl>
      <p>检测耗时 {{ provider.latency_ms }}ms · {{ provider.cache_hit ? '缓存结果' : '实时探测' }} · {{ checkedAt(provider.checked_at) }}<span v-if="provider.error_code"> · {{ provider.error_code }}</span></p>
    </div>
    <p v-else class="muted">正在核对模型发现、结构化输出、流式输出与用量统计…</p>
  </section>
  <section class="admin-ai-module-grid"><RouterLink v-for="item in modules.filter((entry) => auth.has(entry.permission))" :key="item.title" :to="item.route" class="admin-ai-module-card" :class="item.tone"><span>{{ item.icon }}</span><div><h2>{{ item.title }}</h2><p>{{ item.copy }}</p></div><b>进入 →</b></RouterLink></section>
  <section class="admin-ai-workflow admin-panel"><header><div><p class="eyebrow">SAFE RELEASE</p><h2>AI 配置如何安全上线</h2></div><RouterLink to="/admin/approval-requests">打开审批中心</RouterLink></header><ol><li><span>1</span><div><strong>创建草稿</strong><small>修改 Prompt、Skill、工具或知识</small></div></li><li><span>2</span><div><strong>自动与人工评估</strong><small>验证事实、权限、安全与成本</small></div></li><li><span>3</span><div><strong>独立审批</strong><small>高风险变更由另一位管理员复核</small></div></li><li><span>4</span><div><strong>发布并观测</strong><small>灰度生效，异常时快速回滚</small></div></li></ol></section>
</section></template>
