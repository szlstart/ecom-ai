<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import {
  claimSupportTicket,
  getSupportWorkspace,
  listSupportMessages,
  listSupportTickets,
  putSupportReadCursor,
  sendSupportMessage,
  type SupportTicket,
  type SupportWorkspace,
} from '@/api/admin-support'
import { errorMessage } from '@/api/http'
import {
  getMerchantExclusiveConversation,
  listMerchantExclusiveMessages,
  putMerchantExclusiveReadCursor,
  sendMerchantExclusiveMessage,
} from '@/api/merchant-support'
import type { ChatMessage } from '@/api/messaging'
import AgentTracePanel from '@/components/messaging/AgentTracePanel.vue'
import ChatMessageContent from '@/components/messaging/ChatMessageContent.vue'
import { RealtimeConnection, type RealtimeEvent, type RealtimeState } from '@/api/realtime'
import { useAdminAuthStore } from '@/stores/admin-auth'

defineProps<{ storeName?: string }>()

const auth = useAdminAuthStore()
const open = ref(false)
const loading = ref(false)
const sending = ref(false)
const error = ref('')
const draft = ref('')
const tickets = ref<SupportTicket[]>([])
const selectedKey = ref('exclusive')
const workspace = ref<SupportWorkspace | null>(null)
const messages = ref<ChatMessage[]>([])
const exclusiveMessages = ref<ChatMessage[]>([])
const exclusivePreviousCursor = ref<string | null>(null)
const supportPreviousCursor = ref<string | null>(null)
const loadingEarlier = ref(false)
const exclusiveConversationId = ref('')
const exclusiveUnread = ref(0)
const timeline = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const connectionState = ref<RealtimeState>('polling')
const shaking = ref(false)
const selectedTraceRunId = ref<string | null>(null)
let realtime: RealtimeConnection | undefined
let pollingTimer: number | undefined
let refreshTimer: number | undefined
let shakeTimer: number | undefined
let initialized = false

const activeTicket = computed(() => tickets.value.find((item) => item.ticket_id === selectedKey.value) ?? null)
const activeMessages = computed(() => selectedKey.value === 'exclusive' ? exclusiveMessages.value : messages.value)
const unreadCount = computed(() => exclusiveUnread.value + tickets.value.reduce((total, item) => total + item.unread_count, 0))
const title = computed(() => selectedKey.value === 'exclusive' ? '专属客服' : workspace.value?.user.nickname || '顾客咨询')
const subtitle = computed(() => {
  if (selectedKey.value === 'exclusive') return 'AI 经营助理 · 默认只读'
  return activeTicket.value ? statusLabel(activeTicket.value.ticket_status) : '顾客咨询'
})

function token() { return auth.accessToken! }
function statusLabel(value: string) {
  return ({ queued: '等待接待', assigned: '已分配', active: '正在沟通', waiting_user: '等待顾客', resolved: '已解决', closed: '已关闭' } as Record<string, string>)[value] ?? value
}
function isMine(item: ChatMessage) {
  return selectedKey.value === 'exclusive' ? item.sender_type === 'user' : item.sender_type === 'human'
}
function traceRunId(item: ChatMessage): string | null {
  const trace = item.content?.execution_trace
  const value = trace && typeof trace === 'object' && !Array.isArray(trace)
    ? (trace as Record<string, unknown>).run_id
    : null
  return typeof value === 'string' ? value : null
}
async function scrollBottom() { await nextTick(); timeline.value?.scrollTo({ top: timeline.value.scrollHeight }) }

async function loadTickets() {
  const previousUnread = unreadCount.value
  try {
    tickets.value = (await listSupportTickets({ queueType: 'store' }, token())).data.items
    if (initialized && unreadCount.value > previousUnread) shake()
  }
  catch (cause) { error.value = errorMessage(cause) }
}

async function loadExclusive(markRead = open.value && selectedKey.value === 'exclusive') {
  loading.value = true; error.value = ''
  try {
    const previousUnread = exclusiveUnread.value
    const conversation = (await getMerchantExclusiveConversation(token())).data
    exclusiveConversationId.value = conversation.conversation_id
    exclusiveUnread.value = conversation.unread_count
    if (initialized && exclusiveUnread.value > previousUnread) shake()
    const history = (await listMerchantExclusiveMessages(token())).data
    exclusiveMessages.value = history.items
    exclusivePreviousCursor.value = history.previous_cursor
    const lastMessage = exclusiveMessages.value.at(-1)
    if (markRead && lastMessage && exclusiveUnread.value) {
      exclusiveUnread.value = (await putMerchantExclusiveReadCursor(lastMessage, token())).data.unread_count
    }
    await scrollBottom()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function selectConversation(key: string) {
  selectedKey.value = key; workspace.value = null; messages.value = []; supportPreviousCursor.value = null; selectedTraceRunId.value = null; error.value = ''
  if (key === 'exclusive') { await loadExclusive(); return }
  const ticket = tickets.value.find((item) => item.ticket_id === key)
  if (!ticket) return
  loading.value = true
  try {
    if (ticket.ticket_status === 'queued') {
      const claimed = (await claimSupportTicket(ticket, token())).data
      Object.assign(ticket, claimed)
    }
    const [workspaceResult, messageResult] = await Promise.all([
      getSupportWorkspace(ticket.ticket_id, token()),
      listSupportMessages(ticket.conversation_id, token()),
    ])
    workspace.value = workspaceResult.data
    messages.value = messageResult.data.items
    supportPreviousCursor.value = messageResult.data.previous_cursor
    const lastMessage = messages.value.at(-1)
    if (lastMessage && ticket.unread_count) {
      ticket.unread_count = (await putSupportReadCursor(ticket.conversation_id, lastMessage, token())).data.unread_count
    }
    await scrollBottom()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function loadEarlier() {
  const cursor = selectedKey.value === 'exclusive' ? exclusivePreviousCursor.value : supportPreviousCursor.value
  const active = activeTicket.value
  const element = timeline.value
  if (!cursor || !element || loadingEarlier.value || (selectedKey.value !== 'exclusive' && !active)) return
  loadingEarlier.value = true
  const previousHeight = element.scrollHeight
  const previousTop = element.scrollTop
  try {
    const page = selectedKey.value === 'exclusive'
      ? (await listMerchantExclusiveMessages(token(), { cursor })).data
      : (await listSupportMessages(active!.conversation_id, token(), { cursor })).data
    const target = selectedKey.value === 'exclusive' ? exclusiveMessages : messages
    const known = new Set(target.value.map((item) => item.message_id))
    target.value = [...page.items.filter((item) => !known.has(item.message_id)), ...target.value]
    if (selectedKey.value === 'exclusive') exclusivePreviousCursor.value = page.previous_cursor
    else supportPreviousCursor.value = page.previous_cursor
    await nextTick()
    element.scrollTop = previousTop + element.scrollHeight - previousHeight
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loadingEarlier.value = false }
}

async function refreshActiveMessages() {
  try {
    const activeKey = selectedKey.value
    const target = activeKey === 'exclusive' ? exclusiveMessages : messages
    const afterSequence = target.value.at(-1)?.sequence_no ?? 0
    const active = activeTicket.value
    if (activeKey !== 'exclusive' && !active) return
    const shouldScroll = !timeline.value || timeline.value.scrollHeight - timeline.value.scrollTop - timeline.value.clientHeight < 90
    const page = activeKey === 'exclusive'
      ? (await listMerchantExclusiveMessages(token(), { afterSequence })).data
      : (await listSupportMessages(active!.conversation_id, token(), { afterSequence })).data
    if (selectedKey.value !== activeKey) return
    const known = new Set(target.value.map((item) => item.message_id))
    target.value = [...target.value, ...page.items.filter((item) => !known.has(item.message_id))]
    if (shouldScroll) await scrollBottom()
  } catch (cause) { error.value = errorMessage(cause) }
}

async function show() {
  open.value = true
  await Promise.all([loadTickets(), loadExclusive()])
}

function close() {
  open.value = false
  void nextTick(() => trigger.value?.focus())
}

function shake() {
  if (open.value) return
  shaking.value = false
  window.requestAnimationFrame(() => { shaking.value = true })
  if (shakeTimer) window.clearTimeout(shakeTimer)
  shakeTimer = window.setTimeout(() => { shaking.value = false }, 700)
}

function scheduleRefresh() {
  if (refreshTimer) return
  refreshTimer = window.setTimeout(() => {
    refreshTimer = undefined
    void Promise.all([loadTickets(), refreshActiveMessages()])
  }, 120)
}

function handleRealtime(event: RealtimeEvent) {
  if (event.type === 'unread.updated' && String(event.data.conversation_id ?? '') === exclusiveConversationId.value) {
    const unread = Number(event.data.conversation_unread)
    if (Number.isFinite(unread) && unread >= 0) exclusiveUnread.value = unread
  }
  if (event.type === 'message.created') {
    const conversationId = String(event.data.conversation_id ?? '')
    const message = event.data.message as { sender_type?: string } | undefined
    const incoming = conversationId === exclusiveConversationId.value
      ? message?.sender_type !== 'user'
      : message?.sender_type !== 'human'
    if (incoming) shake()
  }
  if (['message.created', 'unread.updated', 'support.ticket.updated'].includes(event.type)) scheduleRefresh()
}

async function send() {
  const text = draft.value.trim()
  if (!text || sending.value) return
  sending.value = true; error.value = ''
  try {
    if (selectedKey.value === 'exclusive') {
      const sent = (await sendMerchantExclusiveMessage(text, token())).data
      exclusiveMessages.value.push(sent)
    } else if (activeTicket.value) {
      messages.value.push((await sendSupportMessage(activeTicket.value.conversation_id, text, token())).data)
    }
    draft.value = ''
    await scrollBottom()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { sending.value = false }
}

onMounted(async () => {
  await Promise.all([loadTickets(), loadExclusive(false)])
  initialized = true
  realtime = new RealtimeConnection({
    audience: 'admin', token, onEvent: handleRealtime,
    onState: (state) => { connectionState.value = state },
    beforeReconnect: () => Promise.all([loadTickets(), refreshActiveMessages()]).then(() => undefined),
  })
  realtime.start()
  pollingTimer = window.setInterval(() => void Promise.all([loadTickets(), refreshActiveMessages()]), 10_000)
})
onBeforeUnmount(() => {
  realtime?.stop()
  if (pollingTimer) window.clearInterval(pollingTimer)
  if (refreshTimer) window.clearTimeout(refreshTimer)
  if (shakeTimer) window.clearTimeout(shakeTimer)
})
</script>

<template>
  <button ref="trigger" class="merchant-message-trigger" :class="{ 'message-arrival-shake': shaking }" type="button" aria-haspopup="dialog" @click="show">
    <span aria-hidden="true">💬</span><span>消息</span><b v-if="unreadCount" :aria-label="`${unreadCount} 条未读消息`">{{ unreadCount > 99 ? '99+' : unreadCount }}</b>
  </button>
  <Teleport to="body">
    <div v-if="open" class="merchant-message-overlay" @mousedown.self="close" @keydown.esc="close">
      <section class="merchant-message-window" role="dialog" aria-modal="true" aria-label="商家消息中心" tabindex="-1">
        <aside class="merchant-chat-list">
          <header><div><strong>消息</strong><small><span class="connection-dot" :class="connectionState" />{{ unreadCount ? `${unreadCount} 条未读` : '消息已读' }}</small></div><button type="button" aria-label="关闭消息中心" @click="close">×</button></header>
          <button class="merchant-chat-item pinned" :class="{ active: selectedKey === 'exclusive' }" type="button" @click="selectConversation('exclusive')">
            <span class="merchant-chat-avatar platform">专</span><span><strong>专属客服 <em>置顶</em></strong><small>AI 经营助理 · 可随时转人工</small></span><i v-if="exclusiveUnread" class="merchant-chat-unread">{{ exclusiveUnread > 99 ? '99+' : exclusiveUnread }}</i>
          </button>
          <p v-if="!tickets.length" class="merchant-chat-empty">暂时没有顾客咨询</p>
          <button v-for="ticket in tickets" :key="ticket.ticket_id" class="merchant-chat-item" :class="{ active: selectedKey === ticket.ticket_id }" type="button" @click="selectConversation(ticket.ticket_id)">
            <span class="merchant-chat-avatar">客</span><span><strong>{{ ticket.handoff_summary || '顾客咨询' }}</strong><small>{{ statusLabel(ticket.ticket_status) }} · {{ new Date(ticket.updated_at).toLocaleString('zh-CN') }}</small></span><i v-if="ticket.unread_count" class="merchant-chat-unread">{{ ticket.unread_count > 99 ? '99+' : ticket.unread_count }}</i>
          </button>
        </aside>
        <main class="merchant-chat-main">
          <header><div><strong>{{ title }}</strong><small>{{ subtitle }}</small></div></header>
          <p v-if="error" class="merchant-chat-error">{{ error }}</p>
          <div ref="timeline" class="merchant-chat-timeline">
            <button v-if="selectedKey === 'exclusive' ? exclusivePreviousCursor : supportPreviousCursor" type="button" class="message-history-button" :disabled="loadingEarlier" @click="loadEarlier">{{ loadingEarlier ? '正在读取更早消息…' : '加载更早消息' }}</button>
            <div v-if="selectedKey === 'exclusive' && !activeMessages.length && !loading" class="merchant-chat-welcome"><span class="merchant-chat-avatar platform">专</span><h2>你好，我是你的专属客服</h2><p>我可以在当前店铺范围内分析商品、订单、库存和经营概况。默认只读，不会替你修改业务数据。</p></div>
            <p v-if="loading" class="merchant-chat-loading">正在读取消息…</p>
            <article v-for="item in activeMessages" :key="item.message_id" class="merchant-chat-bubble-row" :class="{ mine: isMine(item), 'trace-selectable': traceRunId(item), 'trace-selected': traceRunId(item) === selectedTraceRunId }" @click="selectedTraceRunId = traceRunId(item) || selectedTraceRunId"><span v-if="!isMine(item)" class="merchant-chat-avatar">{{ selectedKey === 'exclusive' ? '专' : '客' }}</span><div class="merchant-chat-bubble"><ChatMessageContent :message="item" audience="merchant" /><time>{{ new Date(item.sent_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }}</time></div></article>
          </div>
          <form class="merchant-chat-composer" @submit.prevent="send"><textarea v-model="draft" rows="3" maxlength="4000" :placeholder="selectedKey === 'exclusive' ? '向平台专属客服描述你的问题…' : '回复顾客…'" @keydown.enter.exact.prevent="send" /><footer><small>Enter 发送 · Shift + Enter 换行</small><button :disabled="sending || !draft.trim()">{{ sending ? '发送中…' : '发送' }}</button></footer></form>
        </main>
        <AgentTracePanel :messages="activeMessages" :selected-run-id="selectedTraceRunId" title="AI 协作台" />
      </section>
    </div>
  </Teleport>
</template>
