<script setup lang="ts">
import { computed } from 'vue'

import type { ChatMessage } from '@/api/messaging'
import type { AgentLiveTrace } from '@/api/realtime'

const props = withDefaults(defineProps<{
  messages: ChatMessage[]
  title?: string
  selectedRunId?: string | null
  running?: boolean
  liveTrace?: AgentLiveTrace | null
}>(), {
  title: '透明执行轨迹', selectedRunId: null, running: false, liveTrace: null,
})

type JsonObject = Record<string, unknown>
function objectValue(value: unknown): JsonObject | null { return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : null }
function objectList(value: unknown): JsonObject[] { return Array.isArray(value) ? value.map(objectValue).filter((item): item is JsonObject => item !== null) : [] }
function textValue(value: unknown, fallback = ''): string {
  if (typeof value === 'string') return value.trim() || fallback
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return fallback
}
function pretty(value: unknown): string {
  if (value === undefined) return '未记录'
  if (value === null) return '无'
  if (typeof value === 'string') return value || '空字符串'
  try { return JSON.stringify(value, null, 2) } catch { return String(value) }
}
function statusLabel(value: unknown): string {
  return ({ completed: '已完成', succeeded: '成功', partial: '部分完成', failed: '失败', timed_out: '超时', denied: '已拦截', unknown: '结果未知', reused: '复用结果', running: '执行中', waiting: '等待确认', waiting_confirmation: '等待确认', read: '已读取', not_invoked: '未调用', not_recorded: '未记录' } as Record<string, string>)[String(value ?? '')] || textValue(value, '已记录')
}
function kindLabel(value: unknown): string {
  return ({ plan: '任务规划', supervisor: 'Supervisor 调度', delegation: '子 Agent 委派', context: '上下文读取', memory: '长期记忆', rag: 'RAG 检索', retrieval: '知识检索', tool: '工具调用', security: '安全检查', confirmation: '用户确认', verification: '结果复核', answer: '回答生成' } as Record<string, string>)[String(value ?? '')] || textValue(value, '执行步骤')
}

const selectedTrace = computed<JsonObject | null>(() => {
  const candidates = [...props.messages].reverse().filter((message) => message.sender_type === 'agent' && objectValue(message.content?.execution_trace))
  const selected = props.selectedRunId
    ? candidates.find((message) => {
        const trace = objectValue(message.content?.execution_trace)
        return trace?.run_id === props.selectedRunId || message.content?.run_id === props.selectedRunId
      }) ?? candidates[0]
    : candidates[0]
  return objectValue(selected?.content?.execution_trace)
})
const steps = computed(() => objectList(selectedTrace.value?.steps))
const toolCalls = computed(() => objectList(selectedTrace.value?.tool_calls))
const orchestration = computed(() => objectValue(selectedTrace.value?.orchestration_trace))
const goalLedger = computed(() => objectList(orchestration.value?.goal_ledger ?? selectedTrace.value?.goal_ledger))
const subtasks = computed(() => objectList(orchestration.value?.subtasks ?? selectedTrace.value?.subtasks))
const delegations = computed(() => objectList(orchestration.value?.delegations))
const contextTrace = computed(() => objectValue(selectedTrace.value?.context_trace))
const knowledgeTrace = computed(() => objectValue(selectedTrace.value?.knowledge_trace))
const memoryTrace = computed(() => objectValue(selectedTrace.value?.memory_trace))
const modelInvocation = computed(() => objectValue(selectedTrace.value?.model_invocation))
const modelInvocationStatus = computed(() => {
  const invocation = modelInvocation.value
  if (!invocation) return '未记录'
  if (invocation.status !== undefined) return statusLabel(invocation.status)
  return ['model', 'input_tokens', 'output_tokens', 'total_tokens', 'model_latency_ms', 'first_token_latency_ms']
    .some((key) => invocation[key] !== undefined) ? '已调用' : '未记录'
})
const sourceIds = computed(() => Array.isArray(selectedTrace.value?.source_ids) ? selectedTrace.value!.source_ids : [])
const citedSourceIds = computed(() => Array.isArray(selectedTrace.value?.cited_source_ids) ? selectedTrace.value!.cited_source_ids : [])
function stepToolCall(step: JsonObject): JsonObject | null { return objectValue(step.tool_call) }
function stepFacts(step: JsonObject): Array<[string, string]> {
  const facts: Array<[string, string]> = []
  const values: Array<[string, unknown]> = [
    ['类型', kindLabel(step.kind)], ['状态', statusLabel(step.status)], ['执行 Agent', step.specialist],
    ['委派编号', step.delegation_id], ['目标', step.objective], ['工具', step.tool_code],
    ['允许工具', step.allowed_tools], ['委派深度', step.depth], ['工具调用次数', step.tool_calls],
    ['耗时', typeof step.latency_ms === 'number' ? `${step.latency_ms} ms` : undefined],
    ['Token', step.tokens_used], ['返回数量', step.result_count], ['错误码', step.error_code],
  ]
  values.forEach(([label, value]) => {
    if (value !== undefined && value !== null && value !== '') facts.push([label, Array.isArray(value) ? value.join('、') : String(value)])
  })
  return facts
}
function traceStatus(): string { return props.running ? '思考中' : selectedTrace.value ? statusLabel(selectedTrace.value.status) : '等待中' }
</script>

<template>
  <aside class="agent-reasoning-panel" aria-label="AI 透明执行轨迹">
    <header><div><img src="/ai-avatar.svg" alt="" /><strong>{{ title === '思考过程' ? '透明执行轨迹' : title }}</strong></div><span :class="{ active: running }">{{ traceStatus() }}</span></header>
    <div class="agent-reasoning-body" aria-live="polite">
      <div v-if="running" class="agent-reasoning-sections">
        <details class="plan" open><summary><span>{{ textValue(liveTrace?.label, '模型分析中') }}</span><i aria-hidden="true">⌄</i></summary><div class="trace-copy"><p>{{ textValue(liveTrace?.reasoning) || textValue(liveTrace?.summary, '正在等待模型返回分析摘要和执行事件…') }}</p></div></details>
        <div v-if="!liveTrace?.reasoning" class="agent-reasoning-loading"><i /><i /><i /></div>
      </div>

      <div v-else-if="selectedTrace" class="agent-reasoning-sections">
        <section class="trace-overview">
          <span><small>Agent</small><b>{{ textValue(selectedTrace.agent, '未记录') }}</b></span>
          <span><small>模型</small><b>{{ textValue(selectedTrace.model, '未调用模型') }}</b></span>
          <span><small>意图</small><b>{{ textValue(selectedTrace.intent_label) || textValue(selectedTrace.intent, '未记录') }}</b></span>
          <span><small>轨迹协议</small><b>{{ textValue(selectedTrace.version, '旧版轨迹') }}</b></span>
        </section>

        <details class="plan" open><summary><span>分析与计划</span><i aria-hidden="true">⌄</i></summary><div class="trace-copy">
          <label>用户请求</label><p>{{ textValue(selectedTrace.question, '本轮消息') }}</p>
          <label>分析摘要</label><p>{{ textValue(selectedTrace.analysis_summary, '本轮没有模型分析摘要，以下执行步骤为服务器实际记录。') }}</p>
          <template v-if="Array.isArray(selectedTrace.analysis_details) && selectedTrace.analysis_details.length"><label>分析明细</label><ol><li v-for="(detail, index) in selectedTrace.analysis_details" :key="index">{{ pretty(detail) }}</li></ol></template>
        </div></details>

        <details v-if="orchestration || goalLedger.length || subtasks.length || delegations.length" class="supervisor" open><summary><span>Supervisor 与子 Agent 分配</span><i aria-hidden="true">⌄</i></summary><div class="trace-copy">
          <dl class="trace-facts"><div><dt>编排模式</dt><dd>{{ textValue(orchestration?.mode, 'single_agent') }}</dd></div><div><dt>Supervisor</dt><dd>{{ textValue(orchestration?.supervisor, textValue(selectedTrace.agent)) }}</dd></div><div><dt>计划来源</dt><dd>{{ textValue(orchestration?.planning_source, 'not_recorded') }}</dd></div><div><dt>执行策略</dt><dd>{{ textValue(orchestration?.execution_strategy, 'not_recorded') }}</dd></div><div><dt>目标覆盖</dt><dd>{{ orchestration?.coverage_complete === true ? '覆盖完整' : '未确认完整' }}</dd></div><div><dt>委派数量</dt><dd>{{ textValue(orchestration?.delegation_count, String(delegations.length)) }}</dd></div></dl>
          <template v-if="goalLedger.length"><label>Supervisor 目标账本</label><section v-for="(goal, index) in goalLedger" :key="`goal-${textValue(goal.goal_key, String(index))}`" class="trace-goal-card"><header><b>{{ index + 1 }}. {{ textValue(goal.description, '未记录目标') }}</b><span>{{ textValue(goal.goal_key, `goal_${index + 1}`) }}</span></header><small>分配至：{{ textValue(goal.assigned_task_key, '未分配') }}</small></section></template>
          <section v-for="(task, index) in subtasks" :key="`task-${index}`" class="trace-task-card"><header><b>{{ index + 1 }}. {{ textValue(task.specialist, '领域 Agent') }}</b><span>{{ textValue(task.intent, '子任务') }}</span></header><p>{{ textValue(task.objective, '未记录任务目标') }}</p><small>允许工具：{{ Array.isArray(task.allowed_tools) ? task.allowed_tools.join('、') : textValue(task.allowed_tools, '未记录') }}</small></section>
        </div></details>

        <details v-for="(step, index) in steps" :key="`step-${index}`" class="action" :open="index === 0"><summary><span>{{ index + 1 }}. {{ textValue(step.label, kindLabel(step.kind)) }}</span><em :class="String(step.status ?? '')">{{ statusLabel(step.status) }}</em><i aria-hidden="true">⌄</i></summary><div class="trace-copy">
          <p v-if="textValue(step.summary)">{{ textValue(step.summary) }}</p>
          <dl class="trace-facts"><div v-for="fact in stepFacts(step)" :key="fact[0]"><dt>{{ fact[0] }}</dt><dd>{{ fact[1] }}</dd></div></dl>
          <template v-if="stepToolCall(step)"><label>实际工具参数</label><pre>{{ pretty(stepToolCall(step)?.arguments) }}</pre><label>实际工具结果</label><pre>{{ pretty(stepToolCall(step)?.result) }}</pre></template>
        </div></details>

        <details v-if="toolCalls.length" class="tool"><summary><span>全部工具调用（{{ toolCalls.length }}）</span><i aria-hidden="true">⌄</i></summary><div class="trace-copy"><section v-for="(call, index) in toolCalls" :key="`tool-${index}`" class="trace-call-card"><header><b>{{ index + 1 }}. {{ textValue(call.tool_code, '未知工具') }}</b><span>{{ statusLabel(call.status) }}</span></header><dl class="trace-facts"><div><dt>耗时</dt><dd>{{ textValue(call.latency_ms, '未记录') }} ms</dd></div><div><dt>返回数量</dt><dd>{{ textValue(call.result_count, '未记录') }}</dd></div><div v-if="call.error_code"><dt>错误码</dt><dd>{{ textValue(call.error_code) }}</dd></div></dl><label>参数</label><pre>{{ pretty(call.arguments) }}</pre><label>结果快照</label><pre>{{ pretty(call.result) }}</pre></section></div></details>
        <details class="rag"><summary><span>RAG 知识库检索 · {{ statusLabel(knowledgeTrace?.status ?? 'not_recorded') }}</span><i aria-hidden="true">⌄</i></summary><div class="trace-copy"><pre>{{ pretty(knowledgeTrace ?? { status: 'not_recorded', reason: '该历史消息未保存 RAG 轨迹。' }) }}</pre></div></details>
        <details class="memory"><summary><span>长期记忆读取与命中 · {{ statusLabel(memoryTrace?.status ?? 'not_recorded') }}</span><i aria-hidden="true">⌄</i></summary><div class="trace-copy"><pre>{{ pretty(memoryTrace ?? { status: 'not_recorded', reason: '该历史消息未保存记忆轨迹。' }) }}</pre></div></details>
        <details class="context"><summary><span>短期上下文与连续对话 · {{ statusLabel(contextTrace?.status ?? 'not_recorded') }}</span><i aria-hidden="true">⌄</i></summary><div class="trace-copy"><pre>{{ pretty(contextTrace ?? { status: 'not_recorded', reason: '该历史消息未保存上下文轨迹。' }) }}</pre></div></details>
        <details class="model"><summary><span>模型调用与 Token 指标 · {{ modelInvocationStatus }}</span><i aria-hidden="true">⌄</i></summary><div class="trace-copy"><pre>{{ pretty(modelInvocation ?? { status: 'not_recorded', reason: '该历史消息未保存模型调用轨迹。' }) }}</pre></div></details>
        <details v-if="sourceIds.length || citedSourceIds.length" class="source"><summary><span>数据来源与最终引用</span><i aria-hidden="true">⌄</i></summary><div class="trace-copy"><label>可用来源</label><pre>{{ pretty(sourceIds) }}</pre><label>最终引用</label><pre>{{ pretty(citedSourceIds) }}</pre></div></details>
        <details class="result" open><summary><span>结果</span><i aria-hidden="true">⌄</i></summary><div class="trace-copy"><p>{{ textValue(selectedTrace.result_summary, '已完成本轮处理。') }}</p><dl class="trace-facts"><div><dt>回答方式</dt><dd>{{ textValue(selectedTrace.answer_mode, '未记录') }}</dd></div><div><dt>事实校验</dt><dd>{{ selectedTrace.grounding_verified === true ? '通过' : selectedTrace.grounding_verified === false ? '未通过' : '未报告' }}</dd></div><div><dt>置信度</dt><dd>{{ textValue(selectedTrace.confidence, '未报告') }}</dd></div></dl></div></details>
        <small class="trace-origin-note">模型分析区展示模型服务商实际返回的推理摘要；Agent 分配、工具参数、工具结果、知识检索和记忆命中来自服务器真实执行记录。系统不会伪造模型未提供的内部思维链。</small>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.agent-reasoning-panel{min-width:0;min-height:0;display:grid;grid-template-rows:auto 1fr;color:#171a1f;border-left:1px solid #e5e7eb;background:#fff}.agent-reasoning-panel>header{min-height:64px;padding:11px 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #eceef1;background:#fff}.agent-reasoning-panel>header>div{min-width:0;display:flex;align-items:center;gap:9px}.agent-reasoning-panel>header img{width:31px;height:31px;border-radius:10px}.agent-reasoning-panel>header strong{font-size:.84rem}.agent-reasoning-panel>header>span{padding:5px 9px;color:#68707b;border:1px solid #d9dde3;border-radius:999px;background:#f8f9fa;font-size:.61rem}.agent-reasoning-panel>header>span.active{color:#187a4b;border-color:#b9e3ce;background:#eefaf4}.agent-reasoning-body{padding:12px;overflow-y:auto;background:#fff}.agent-reasoning-sections{display:grid;gap:10px}.agent-reasoning-sections>details{overflow:hidden;border:1px solid #e1e5eb;border-radius:11px;background:#fff}.agent-reasoning-sections>details.plan{border-left:3px solid #476edb}.agent-reasoning-sections>details.supervisor{border-left:3px solid #7049c9}.agent-reasoning-sections>details.action{border-left:3px solid #8f9aac}.agent-reasoning-sections>details.tool{border-left:3px solid #e2872f}.agent-reasoning-sections>details.rag{border-left:3px solid #2774c7}.agent-reasoning-sections>details.memory{border-left:3px solid #a14fac}.agent-reasoning-sections>details.context{border-left:3px solid #4d8997}.agent-reasoning-sections>details.model{border-left:3px solid #555dc4}.agent-reasoning-sections>details.result{border-left:3px solid #2b9168}.agent-reasoning-sections summary{padding:10px 11px;display:grid;grid-template-columns:minmax(0,1fr) auto auto;align-items:center;gap:7px;cursor:pointer;color:#20252c;background:#fafbfc;font-size:.71rem;font-weight:780;list-style:none}.agent-reasoning-sections summary::-webkit-details-marker{display:none}.agent-reasoning-sections summary i{color:#77808d;font-style:normal;transition:transform .18s}.agent-reasoning-sections summary em{padding:3px 6px;color:#66717f;border-radius:999px;background:#edf0f4;font-size:.56rem;font-style:normal}.agent-reasoning-sections summary em.succeeded,.agent-reasoning-sections summary em.completed{color:#207653;background:#e8f7ef}.agent-reasoning-sections summary em.failed,.agent-reasoning-sections summary em.denied{color:#a43b3b;background:#fbeaea}.agent-reasoning-sections details[open] summary i{transform:rotate(180deg)}.trace-copy{padding:11px;display:grid;gap:9px;border-top:1px solid #eceef2}.trace-copy>p,.trace-copy>ol{margin:0;color:#303640;font-size:.7rem;line-height:1.75;white-space:pre-wrap;word-break:break-word}.trace-copy>ol{padding-left:18px}.trace-copy>label{color:#707987;font-size:.6rem;font-weight:800;letter-spacing:.04em}.trace-copy>pre{max-height:320px;margin:0;padding:9px;overflow:auto;color:#27303b;border:1px solid #e4e7ec;border-radius:8px;background:#f7f8fa;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.6rem;line-height:1.55;white-space:pre-wrap;word-break:break-word}.trace-overview{display:grid;grid-template-columns:1fr 1fr;gap:7px}.trace-overview>span{min-width:0;padding:8px;display:grid;gap:3px;border:1px solid #e4e7ec;border-radius:9px;background:#fafbfc}.trace-overview small{color:#818997;font-size:.55rem}.trace-overview b{overflow:hidden;color:#303641;font-size:.64rem;text-overflow:ellipsis;white-space:nowrap}.trace-facts{margin:0;display:grid;gap:5px}.trace-facts>div{display:grid;grid-template-columns:76px minmax(0,1fr);gap:7px;font-size:.62rem}.trace-facts dt{color:#7b8492}.trace-facts dd{margin:0;color:#303741;overflow-wrap:anywhere}.trace-goal-card,.trace-task-card,.trace-call-card{padding:9px;display:grid;gap:7px;border:1px solid #e2e5ea;border-radius:9px;background:#fafbfc}.trace-goal-card{border-color:#d9cdf2;background:#faf8ff}.trace-goal-card header,.trace-task-card header,.trace-call-card header{display:flex;align-items:center;justify-content:space-between;gap:8px}.trace-goal-card header b,.trace-task-card header b,.trace-call-card header b{min-width:0;overflow:hidden;font-size:.66rem;text-overflow:ellipsis;white-space:nowrap}.trace-goal-card header span,.trace-task-card header span,.trace-call-card header span{flex:0 0 auto;color:#5c6780;font-size:.56rem}.trace-task-card p{margin:0;color:#3a424e;font-size:.64rem;line-height:1.55}.trace-goal-card small,.trace-task-card small{color:#7d8694;font-size:.57rem}.trace-origin-note{padding:4px 3px;color:#7c8592;font-size:.58rem;line-height:1.55}.agent-reasoning-loading{padding:8px;display:flex;align-items:center;gap:5px;color:#272c33}.agent-reasoning-loading i{width:5px;height:5px;border-radius:50%;background:#4f5967;animation:reasoning-dot 1.1s ease-in-out infinite}.agent-reasoning-loading i:nth-child(2){animation-delay:.14s}.agent-reasoning-loading i:nth-child(3){animation-delay:.28s}@keyframes reasoning-dot{50%{transform:translateY(-3px);opacity:.35}}@media(max-width:899px){.agent-reasoning-panel{display:none}}
</style>
