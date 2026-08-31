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

const completedReasoning = computed(() => {
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
  if (!trace || typeof trace !== 'object' || Array.isArray(trace)) return ''
  const value = trace as Record<string, unknown>
  if (value.answer_mode !== 'model_grounded' || value.thinking_mode !== 'enabled') return ''
  return typeof value.analysis_summary === 'string'
    ? String(value.analysis_summary).trim()
    : ''
})

const reasoning = computed(() => {
  if (props.running) return props.liveTrace?.reasoning?.trim() ?? ''
  return completedReasoning.value
})
</script>

<template>
  <aside class="agent-reasoning-panel" aria-label="AI 思考过程">
    <header>
      <div><img src="/ai-avatar.svg" alt="" /><strong>{{ title }}</strong></div>
      <span :class="{ active: running }">{{ running ? '思考中' : reasoning ? '已完成' : '等待中' }}</span>
    </header>
    <div class="agent-reasoning-body" aria-live="polite">
      <div v-if="running && !reasoning" class="agent-reasoning-loading">
        <i /><i /><i />
      </div>
      <p v-else-if="reasoning" class="agent-reasoning-text">{{ reasoning }}</p>
    </div>
  </aside>
</template>

<style scoped>
.agent-reasoning-panel{min-width:0;min-height:0;display:grid;grid-template-rows:auto 1fr;color:#171a1f;border-left:1px solid #e5e7eb;background:#fff}.agent-reasoning-panel>header{min-height:64px;padding:11px 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #eceef1;background:#fff}.agent-reasoning-panel>header>div{display:flex;align-items:center;gap:9px}.agent-reasoning-panel>header img{width:31px;height:31px;border-radius:10px}.agent-reasoning-panel>header strong{font-size:.84rem}.agent-reasoning-panel>header>span{padding:5px 9px;color:#68707b;border:1px solid #d9dde3;border-radius:999px;background:#f8f9fa;font-size:.61rem}.agent-reasoning-panel>header>span.active{color:#187a4b;border-color:#b9e3ce;background:#eefaf4}.agent-reasoning-body{padding:18px;overflow-y:auto;background:#fff}.agent-reasoning-text{margin:0;color:#20242a;font-size:.75rem;line-height:1.85;white-space:pre-wrap;word-break:break-word}.agent-reasoning-loading{display:flex;align-items:center;gap:5px;color:#272c33}.agent-reasoning-loading i{width:5px;height:5px;border-radius:50%;background:#4f5967;animation:reasoning-dot 1.1s ease-in-out infinite}.agent-reasoning-loading i:nth-child(2){animation-delay:.14s}.agent-reasoning-loading i:nth-child(3){animation-delay:.28s}@keyframes reasoning-dot{50%{transform:translateY(-3px);opacity:.35}}@media(max-width:899px){.agent-reasoning-panel{display:none}}
</style>
