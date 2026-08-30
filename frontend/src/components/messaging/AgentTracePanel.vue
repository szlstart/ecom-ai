<script setup lang="ts">
import { computed } from 'vue'

import type { ChatMessage } from '@/api/messaging'

type TraceStep = {
  kind?: string
  label?: string
  status?: string
  tool_code?: string
  specialist?: string
  delegation_id?: string
  latency_ms?: number
  tool_calls?: number
  tokens_used?: number
}

const props = withDefaults(defineProps<{
  messages: ChatMessage[]
  title?: string
  selectedRunId?: string | null
}>(), { title: 'AI 工作记录', selectedRunId: null })

const trace = computed<Record<string, unknown> | null>(() => {
  const candidates = [...props.messages].reverse().filter((item) => {
    const value = item.content?.execution_trace
    return item.sender_type === 'agent' && value && typeof value === 'object' && !Array.isArray(value)
  })
  const message = props.selectedRunId
    ? candidates.find((item) => {
      const value = item.content?.execution_trace as Record<string, unknown> | undefined
      return value?.run_id === props.selectedRunId || item.content?.run_id === props.selectedRunId
    }) ?? candidates[0]
    : candidates[0]
  const value = message?.content?.execution_trace
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
})
const steps = computed<TraceStep[]>(() => Array.isArray(trace.value?.steps)
  ? trace.value.steps.filter((item): item is TraceStep => Boolean(item && typeof item === 'object'))
  : [])
const sources = computed(() => {
  const value = trace.value?.cited_source_ids ?? trace.value?.source_ids
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
})
const statusText = computed(() => trace.value?.status === 'completed' ? '已完成' : '处理中')
const modeText = computed(() => {
  const answerMode = trace.value?.answer_mode === 'model_grounded'
    ? 'Kimi 证据约束生成'
    : trace.value?.answer_mode === 'model_routed_safe_reply'
      ? 'Kimi 意图路由 · 安全快捷回复'
      : '安全规则回复'
  return trace.value?.orchestration_mode === 'multi_agent' ? `多 Agent 协作 · ${answerMode}` : answerMode
})
function stepIcon(kind?: string): string {
  return ({ plan: '◇', supervisor: '✦', delegation: '↗', tool: '⌘', rag: '⌕', retrieval: '⌕', context: '◫', memory: '◉', answer: '✓', security: '⚑' } as Record<string, string>)[kind ?? ''] ?? '·'
}
function stepStatus(value?: string): string {
  return ({ completed: '完成', succeeded: '成功', reused: '复用', partial: '部分完成', failed: '失败', skipped: '跳过' } as Record<string, string>)[value ?? ''] ?? '完成'
}
function stepMeta(step: TraceStep): string {
  const values: string[] = []
  if (step.tool_code) values.push(`工具 ${step.tool_code}`)
  if (step.specialist) values.push(`Agent ${step.specialist}`)
  if (typeof step.tool_calls === 'number') values.push(`${step.tool_calls} 次工具`)
  if (typeof step.latency_ms === 'number') values.push(`${step.latency_ms} ms`)
  if (typeof step.tokens_used === 'number' && step.tokens_used > 0) values.push(`${step.tokens_used} tokens`)
  return values.join(' · ')
}
</script>

<template>
  <aside class="agent-trace-panel" aria-label="AI 安全执行记录">
    <header><div><small>受控 Agent Harness</small><strong>{{ title }}</strong></div><span :class="{ active: trace }">{{ trace ? statusText : '等待 AI' }}</span></header>
    <div v-if="trace" class="agent-trace-body">
      <section class="agent-trace-summary"><span class="agent-trace-orb">✦</span><div><strong>{{ String(trace.agent || '智能客服') }}</strong><small>{{ modeText }}</small></div></section>
      <dl>
        <div><dt>任务</dt><dd>{{ String(trace.intent || '安全问答') }}</dd></div>
        <div><dt>置信度</dt><dd>{{ String(trace.confidence || '未标注') }}</dd></div>
        <div><dt>运行编号</dt><dd>{{ String(trace.run_id || '—').slice(-12) }}</dd></div>
      </dl>
      <section class="agent-trace-steps"><h3>执行时间线</h3><ol><li v-for="(step, index) in steps" :key="step.delegation_id || `${index}-${step.label}`" :class="step.status"><i>{{ stepIcon(step.kind) }}</i><div><strong>{{ step.label || '受控步骤' }}</strong><small v-if="stepMeta(step)">{{ stepMeta(step) }}</small></div><span>{{ stepStatus(step.status) }}</span></li></ol></section>
      <section class="agent-trace-sources"><h3>可信来源</h3><div v-if="sources.length"><code v-for="source in sources" :key="source">{{ source }}</code></div><p v-else>本次回复未引用外部知识文档。</p></section>
      <footer><span>🔒</span><p><strong>隐私保护</strong><small>这里只展示可审计摘要，不展示模型原始思维链、隐藏提示词或敏感数据。</small></p></footer>
    </div>
    <div v-else class="agent-trace-empty"><span>✦</span><h3>暂无 AI 执行记录</h3><p>发送问题后，这里会展示使用的能力、工具、来源和安全状态。</p></div>
  </aside>
</template>

<style scoped>
.agent-trace-panel{min-width:0;min-height:0;display:grid;grid-template-rows:auto 1fr;color:#dfe8ff;border-left:1px solid rgb(143 168 232 / 18%);background:radial-gradient(circle at 90% 0,rgb(91 76 210 / 20%),transparent 34%),#10182a}
.agent-trace-panel>header{min-height:74px;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid rgb(147 166 219 / 14%)}
.agent-trace-panel>header div{display:grid;gap:3px}.agent-trace-panel>header small{color:#8290b3;font-size:.62rem;letter-spacing:.08em;text-transform:uppercase}.agent-trace-panel>header strong{font-size:.9rem}.agent-trace-panel>header>span{padding:5px 8px;color:#8e9ab8;border:1px solid #303b57;border-radius:999px;font-size:.62rem}.agent-trace-panel>header>span.active{color:#76e1b3;border-color:#28664f;background:#173c31}
.agent-trace-body{padding:16px;display:grid;align-content:start;gap:15px;overflow-y:auto}.agent-trace-summary{padding:13px;display:flex;align-items:center;gap:10px;border:1px solid rgb(121 145 211 / 20%);border-radius:14px;background:rgb(255 255 255 / 4%)}.agent-trace-orb{width:38px;height:38px;display:grid;place-items:center;border-radius:12px;background:linear-gradient(145deg,#776be4,#3a70cd);box-shadow:0 8px 22px rgb(65 87 206 / 30%)}.agent-trace-summary div{display:grid;gap:3px}.agent-trace-summary small{color:#8fa0c4;font-size:.64rem}
dl{margin:0;display:grid;gap:7px}dl>div{display:flex;justify-content:space-between;gap:10px}dt{color:#7f8dac;font-size:.64rem}dd{margin:0;overflow:hidden;color:#cbd7f4;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.64rem;text-overflow:ellipsis;white-space:nowrap}
h3{margin:0 0 9px;color:#8c9abc;font-size:.65rem;letter-spacing:.05em}.agent-trace-steps ol{margin:0;padding:0;display:grid;gap:8px;list-style:none}.agent-trace-steps li{display:grid;grid-template-columns:28px minmax(0,1fr) auto;align-items:center;gap:8px}.agent-trace-steps i{width:28px;height:28px;display:grid;place-items:center;color:#94a8e8;border:1px solid #354466;border-radius:9px;background:#1b263e;font-style:normal}.agent-trace-steps li>div{min-width:0;display:grid;gap:2px}.agent-trace-steps strong{font-size:.68rem}.agent-trace-steps small{overflow:hidden;color:#7584a6;font-size:.58rem;text-overflow:ellipsis;white-space:nowrap}.agent-trace-steps li>span{color:#67cfa3;font-size:.58rem}.agent-trace-steps li.failed>span{color:#ff8a8a}.agent-trace-steps li.partial>span{color:#f2bd64}
.agent-trace-sources>div{display:flex;flex-wrap:wrap;gap:5px}.agent-trace-sources code{max-width:100%;padding:5px 7px;overflow:hidden;color:#aabcf0;border:1px solid #34466e;border-radius:7px;background:#172239;font-size:.58rem;text-overflow:ellipsis}.agent-trace-sources p{margin:0;color:#7584a5;font-size:.64rem;line-height:1.5}.agent-trace-body>footer{padding:11px;display:flex;gap:8px;border:1px solid rgb(68 188 142 / 18%);border-radius:12px;background:rgb(38 112 86 / 11%)}.agent-trace-body>footer p{margin:0;display:grid;gap:3px}.agent-trace-body>footer strong{color:#8edbbd;font-size:.65rem}.agent-trace-body>footer small{color:#8193ac;font-size:.59rem;line-height:1.45}
.agent-trace-empty{align-self:center;padding:24px;text-align:center}.agent-trace-empty>span{width:52px;height:52px;margin:auto;display:grid;place-items:center;color:#adbbdf;border:1px solid #33405e;border-radius:17px;background:#182238}.agent-trace-empty h3{margin:13px 0 6px;color:#d2dbef;font-size:.8rem}.agent-trace-empty p{margin:0;color:#7483a4;font-size:.65rem;line-height:1.55}
@media(max-width:1279px){.agent-trace-panel{display:none}}
</style>
