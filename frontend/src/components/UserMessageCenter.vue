<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { errorMessage } from '@/api/http'
import {
  ensureExclusiveConversation,
  listConversations,
  type Conversation,
} from '@/api/messaging'
import { RealtimeConnection, type AgentLiveTrace, type RealtimeEvent, type RealtimeState } from '@/api/realtime'
import ConversationPage from '@/pages/ConversationPage.vue'
import AgentTracePanel from '@/components/messaging/AgentTracePanel.vue'
import type { ChatMessage } from '@/api/messaging'
import { useMessageCenterStore } from '@/stores/message-center'
import { useUserAuthStore } from '@/stores/user-auth'

withDefaults(defineProps<{ standalone?: boolean }>(), { standalone: false })

const auth = useUserAuthStore()
const center = useMessageCenterStore()
const route = useRoute()
const items = ref<Conversation[]>([])
const loading = ref(false)
const error = ref('')
const connectionState = ref<RealtimeState>('polling')
const shaking = ref(false)
const traceMessages = ref<ChatMessage[]>([])
const selectedTraceRunId = ref<string | null>(null)
const traceRunning = ref(false)
const liveTrace = ref<AgentLiveTrace | null>(null)
let realtime: RealtimeConnection | undefined
let refreshTimer: number | undefined
let pollingTimer: number | undefined
let shakeTimer: number | undefined
let initialized = false

const exclusive = computed(() => items.value.find((item) => item.conversation_type === 'exclusive') ?? null)
const stores = computed(() => items.value.filter((item) => item.conversation_type === 'store'))
const selected = computed(() => items.value.find((item) => item.conversation_id === center.selectedConversationId) ?? null)
const totalUnread = computed(() => items.value.reduce((total, item) => total + item.unread_count, 0))

function token(): string {
  if (!auth.accessToken) throw new Error('missing user token')
  return auth.accessToken
}
function unreadLabel(value: number): string { return value > 99 ? '99+' : String(value) }
function timeLabel(value: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  return date.toDateString() === new Date().toDateString()
    ? new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(date)
    : new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit' }).format(date)
}
function shake() {
  if (route.path.startsWith('/messages')) return
  shaking.value = false
  window.requestAnimationFrame(() => { shaking.value = true })
  if (shakeTimer) window.clearTimeout(shakeTimer)
  shakeTimer = window.setTimeout(() => { shaking.value = false }, 700)
}
async function load(ensureExclusive = false) {
  if (!auth.isAuthenticated) return
  loading.value = ensureExclusive
  error.value = ''
  try {
    if (ensureExclusive) await ensureExclusiveConversation(token())
    const previousUnread = totalUnread.value
    items.value = (await listConversations(token())).data.items
    if (!center.selectedConversationId || !items.value.some((item) => item.conversation_id === center.selectedConversationId)) {
      center.selectedConversationId = exclusive.value?.conversation_id ?? items.value[0]?.conversation_id ?? null
    }
    if (initialized && totalUnread.value > previousUnread) shake()
    initialized = true
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}
function scheduleRefresh() {
  if (refreshTimer) return
  refreshTimer = window.setTimeout(() => {
    refreshTimer = undefined
    void load()
  }, 120)
}
function handleRealtime(event: RealtimeEvent) {
  if (event.type === 'unread.updated') {
    const conversationId = String(event.data.conversation_id ?? '')
    const unread = Number(event.data.conversation_unread)
    const target = items.value.find((item) => item.conversation_id === conversationId)
    if (target && Number.isFinite(unread) && unread >= 0) target.unread_count = unread
  }
  if (event.type === 'message.created') {
    const message = event.data.message as { sender_type?: string } | undefined
    if (message?.sender_type !== 'user') shake()
  }
  if (['message.created', 'unread.updated', 'support.status.updated'].includes(event.type)) scheduleRefresh()
}
function selectConversation(item: Conversation) {
  center.selectedConversationId = item.conversation_id
  traceMessages.value = []
  selectedTraceRunId.value = null
}
async function conversationDeleted(conversationId: string) {
  items.value = items.value.filter((item) => item.conversation_id !== conversationId)
  center.selectedConversationId = null
  traceMessages.value = []
  selectedTraceRunId.value = null
  await load(true)
}
function updateReadCursor(conversationId: string, unreadCount: number) {
  const item = items.value.find((candidate) => candidate.conversation_id === conversationId)
  if (item) item.unread_count = unreadCount
}
watch(() => auth.isAuthenticated, (value) => {
  if (value) void load(true)
  else { items.value = [] }
})

onMounted(async () => {
  if (!auth.isAuthenticated) return
  await load(true)
  realtime = new RealtimeConnection({
    audience: 'user', token, onEvent: handleRealtime,
    onState: (state) => { connectionState.value = state },
    beforeReconnect: () => load(),
  })
  realtime.start()
  pollingTimer = window.setInterval(() => void load(), 10_000)
})
onBeforeUnmount(() => {
  realtime?.stop()
  if (refreshTimer) window.clearTimeout(refreshTimer)
  if (pollingTimer) window.clearInterval(pollingTimer)
  if (shakeTimer) window.clearTimeout(shakeTimer)
})
</script>

<template>
  <RouterLink
    v-if="!standalone"
    class="user-message-trigger storefront-nav-entry"
    :class="{ 'message-arrival-shake': shaking }"
    to="/messages"
    aria-label="消息"
  >
    <span aria-hidden="true">💬</span>
    <span class="nav-entry-label">消息</span>
    <b v-if="totalUnread" :aria-label="`${totalUnread} 条未读消息`">{{ unreadLabel(totalUnread) }}</b>
  </RouterLink>
  <div v-else class="message-page-surface user-message-page-surface">
      <section class="merchant-message-window user-message-window" aria-label="用户消息中心">
        <aside class="merchant-chat-list user-chat-list">
          <header><div><strong>会话列表</strong><small><span class="connection-dot" :class="connectionState" />{{ totalUnread ? `${totalUnread} 条未读` : '消息已读' }}</small></div></header>
          <p v-if="error" class="merchant-chat-error">{{ error }}</p>
          <p v-if="loading && !items.length" class="merchant-chat-empty">正在读取会话…</p>
          <button v-if="exclusive" class="merchant-chat-item pinned" :class="{ active: selected?.conversation_id === exclusive.conversation_id }" type="button" @click="selectConversation(exclusive)">
            <span class="merchant-chat-avatar platform"><img src="/ai-avatar.svg" alt="" /></span><span><strong>专属客服 <em>置顶</em></strong><small>{{ exclusive.last_message_preview || '平台规则、订单、物流与售后' }}</small></span><i v-if="exclusive.unread_count" class="user-chat-unread" :aria-label="`${exclusive.unread_count} 条未读消息`">{{ unreadLabel(exclusive.unread_count) }}</i>
          </button>
          <div v-if="!stores.length && !loading" class="merchant-chat-empty message-empty-guide">
            <span aria-hidden="true">⌕</span><strong>还没有店铺咨询</strong><small>在商品详情页联系商家后，会话会出现在这里。</small><RouterLink to="/">去逛逛</RouterLink>
          </div>
          <button v-for="item in stores" :key="item.conversation_id" class="merchant-chat-item" :class="{ active: selected?.conversation_id === item.conversation_id }" type="button" @click="selectConversation(item)">
            <span class="merchant-chat-avatar">{{ item.title.slice(0, 1) }}</span><span><strong>{{ item.title }}</strong><small>{{ item.last_message_preview || '开始咨询店铺客服' }} · {{ timeLabel(item.last_message_at) }}</small></span><i v-if="item.unread_count" class="user-chat-unread" :aria-label="`${item.unread_count} 条未读消息`">{{ unreadLabel(item.unread_count) }}</i>
          </button>
        </aside>
        <main class="user-chat-main">
          <ConversationPage v-if="center.selectedConversationId" :key="center.selectedConversationId" :conversation-id="center.selectedConversationId" embedded @trace-update="traceMessages = $event" @trace-select="selectedTraceRunId = $event" @trace-running="traceRunning = $event" @trace-progress="liveTrace = $event" @conversation-deleted="conversationDeleted" @read-cursor="updateReadCursor" />
          <div v-else class="merchant-chat-welcome"><span class="merchant-chat-avatar platform"><img src="/ai-avatar.svg" alt="" /></span><h2>消息中心</h2><p>选择左侧会话开始沟通。</p></div>
        </main>
        <AgentTracePanel :messages="traceMessages" :selected-run-id="selectedTraceRunId" :running="traceRunning" :live-trace="liveTrace" />
      </section>
  </div>
</template>
