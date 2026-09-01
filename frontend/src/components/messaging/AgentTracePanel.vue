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
  title: '思考过程',
  selectedRunId: null,
  running: false,
  liveTrace: null,
})

type TraceStep = Record<string, unknown>
type TraceSection = { title: string; text: string; tone: 'plan' | 'action' | 'result' }

const selectedTrace = computed<Record<string, unknown> | null>(() => {
  const candidates = [...props.messages].reverse().filter((message) => {
    const value = message.content?.execution_trace
    return message.sender_type === 'agent' && value && typeof value === 'object' && !Array.isArray(value)
  })
  const selected = props.selectedRunId
    ? candidates.find((message) => {
        const value = message.content?.execution_trace as Record<string, unknown>
        return value.run_id === props.selectedRunId || message.content?.run_id === props.selectedRunId
      }) ?? candidates[0]
    : candidates[0]
  const trace = selected?.content?.execution_trace
  return trace && typeof trace === 'object' && !Array.isArray(trace)
    ? trace as Record<string, unknown>
    : null
})

function chineseText(value: unknown): string {
  const text = typeof value === 'string' ? value.trim() : ''
  return /[\u3400-\u9fff]/.test(text) ? text : ''
}
function intentLabel(trace: Record<string, unknown>): string {
  return chineseText(trace.intent_label) || ({
    general_chat: '理解日常咨询并承接当前话题', product_qa: '核对当前商品信息',
    sku_compare: '比较款式与规格', inventory_lookup: '查询当前库存', policy_qa: '检索服务政策',
    order_explain: '查询并解释当前订单', product_recommend: '筛选商品候选',
    order_lookup: '查询本人订单', logistics_lookup: '查询订单物流', refund_precheck: '检查售后资格',
    human_handoff: '识别人工服务请求', overview: '分析经营概况', catalog: '分析商品运营',
    orders: '分析订单与履约', inventory: '分析库存风险', users: '分析平台用户', stores: '分析平台店铺',
    runtime: '分析 Agent 运行状态', complex_platform_diagnosis: '拆解跨领域平台诊断任务',
    complex_store_diagnosis: '拆解店铺经营诊断任务',
  } as Record<string, string>)[String(trace.intent ?? '')] || '理解问题并限定处理范围'
}
function stepText(step: TraceStep): string {
  const label = chineseText(step.label) || '执行受控处理步骤'
  const summary = chineseText(step.summary)
  const tool = typeof step.tool_code === 'string' && step.tool_code !== 'none' ? step.tool_code : ''
  const status = ({ completed: '已完成', succeeded: '已完成', partial: '部分完成', failed: '执行失败', timed_out: '执行超时', reused: '复用已验证结果' } as Record<string, string>)[String(step.status ?? '')] || '已完成'
  const facts = [summary || `已${label}。`]
  if (tool) facts.push(`调用工具：${tool}；调用前已检查身份、权限和数据范围。`)
  if (typeof step.result_count === 'number') facts.push(`返回 ${step.result_count} 项可用结果。`)
  facts.push(`状态：${status}。`)
  return facts.join('\n')
}

const completedSections = computed<TraceSection[]>(() => {
  const trace = selectedTrace.value
  if (!trace) return []
  const question = chineseText(trace.question) || '本轮消息'
  const providerSummary = chineseText(trace.analysis_summary)
  const plan = providerSummary || `我把“${question}”理解为“${intentLabel(trace)}”。接下来只在当前身份、会话绑定对象和已授权数据范围内核对信息，不把历史对话当作业务事实。`
  const sections: TraceSection[] = [{ title: '分析与计划', text: plan, tone: 'plan' }]
  const steps = Array.isArray(trace.steps)
    ? trace.steps.filter((item): item is TraceStep => Boolean(item && typeof item === 'object' && !Array.isArray(item)))
    : []
  steps.forEach((step, index) => sections.push({
    title: `${index + 1}. ${chineseText(step.label) || '受控处理步骤'}`,
    text: stepText(step),
    tone: 'action',
  }))
  const details = Array.isArray(trace.analysis_details)
    ? trace.analysis_details.map(chineseText).filter(Boolean)
    : []
  if (!steps.length && details.length) sections.push({ title: '核验过程', text: details.join('\n'), tone: 'action' })
  const result = chineseText(trace.result_summary)
    || `已完成本轮处理。聊天区回复只使用通过权限和事实一致性检查的信息${trace.answer_mode === 'deterministic_fallback' ? '；模型不可用时已使用受控降级答案' : ''}。`
  sections.push({ title: '结果', text: result, tone: 'result' })
  return sections
})

const liveSections = computed<TraceSection[]>(() => {
  if (!props.running) return []
  const live = props.liveTrace
  const reasoning = chineseText(live?.reasoning)
  const summary = chineseText(live?.summary)
  return [{
    title: chineseText(live?.label) || '分析进行中',
    text: reasoning || summary || '正在理解当前消息、最近对话的承接关系和可访问的数据范围。',
    tone: 'plan',
  }]
})

const sections = computed(() => props.running ? liveSections.value : completedSections.value)
</script>

<template>
  <aside class="agent-reasoning-panel" aria-label="AI 思考过程">
    <header>
      <div><img src="/ai-avatar.svg" alt="" /><strong>{{ title }}</strong></div>
      <span :class="{ active: running }">{{ running ? '思考中' : sections.length ? '已完成' : '等待中' }}</span>
    </header>
    <div class="agent-reasoning-body" aria-live="polite">
      <div v-if="running && !sections.length" class="agent-reasoning-loading"><i /><i /><i /></div>
      <div v-else-if="sections.length" class="agent-reasoning-sections">
        <details v-for="(section, index) in sections" :key="`${section.title}-${index}`" :class="section.tone" :open="index === 0 || running">
          <summary><span>{{ section.title }}</span><i aria-hidden="true">⌄</i></summary>
          <p>{{ section.text }}</p>
        </details>
        <small>这里展示可核验的分析摘要与实际执行记录，不展示系统密钥、隐藏提示词或模型私有思维链。</small>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.agent-reasoning-panel{min-width:0;min-height:0;display:grid;grid-template-rows:auto 1fr;color:#171a1f;border-left:1px solid #e5e7eb;background:#fff}.agent-reasoning-panel>header{min-height:64px;padding:11px 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #eceef1;background:#fff}.agent-reasoning-panel>header>div{display:flex;align-items:center;gap:9px}.agent-reasoning-panel>header img{width:31px;height:31px;border-radius:10px}.agent-reasoning-panel>header strong{font-size:.84rem}.agent-reasoning-panel>header>span{padding:5px 9px;color:#68707b;border:1px solid #d9dde3;border-radius:999px;background:#f8f9fa;font-size:.61rem}.agent-reasoning-panel>header>span.active{color:#187a4b;border-color:#b9e3ce;background:#eefaf4}.agent-reasoning-body{padding:14px;overflow-y:auto;background:#fff}.agent-reasoning-sections{display:grid;gap:10px}.agent-reasoning-sections>details{overflow:hidden;border:1px solid #e1e5eb;border-radius:11px;background:#fff}.agent-reasoning-sections>details.plan{border-left:3px solid #476edb}.agent-reasoning-sections>details.action{border-left:3px solid #8f9aac}.agent-reasoning-sections>details.result{border-left:3px solid #2b9168}.agent-reasoning-sections summary{padding:10px 11px;display:flex;align-items:center;justify-content:space-between;gap:8px;cursor:pointer;color:#20252c;background:#fafbfc;font-size:.71rem;font-weight:780;list-style:none}.agent-reasoning-sections summary::-webkit-details-marker{display:none}.agent-reasoning-sections summary i{color:#77808d;font-style:normal;transition:transform .18s}.agent-reasoning-sections details[open] summary i{transform:rotate(180deg)}.agent-reasoning-sections p{margin:0;padding:11px;color:#303640;border-top:1px solid #eceef2;font-size:.72rem;line-height:1.78;white-space:pre-wrap;word-break:break-word}.agent-reasoning-sections>small{padding:4px 3px;color:#89919d;font-size:.59rem;line-height:1.55}.agent-reasoning-loading{display:flex;align-items:center;gap:5px;color:#272c33}.agent-reasoning-loading i{width:5px;height:5px;border-radius:50%;background:#4f5967;animation:reasoning-dot 1.1s ease-in-out infinite}.agent-reasoning-loading i:nth-child(2){animation-delay:.14s}.agent-reasoning-loading i:nth-child(3){animation-delay:.28s}@keyframes reasoning-dot{50%{transform:translateY(-3px);opacity:.35}}@media(max-width:899px){.agent-reasoning-panel{display:none}}
</style>
