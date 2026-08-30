<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import {
  claimSupportTicket,
  getAdminAiConversation,
  getSupportWorkspace,
  listAdminAiMessages,
  listSupportConversations,
  listSupportMessages,
  putAdminAiReadCursor,
  putSupportReadCursor,
  resolveSupportTicket,
  sendAdminAiMessage,
  sendSupportMessage,
  type SupportConversation,
  type SupportWorkspace,
} from '@/api/admin-support'
import { errorMessage } from '@/api/http'
import { createClientMessageId, type ChatMessage } from '@/api/messaging'
import { RealtimeConnection, type RealtimeEvent, type RealtimeState } from '@/api/realtime'
import { useAdminAuthStore } from '@/stores/admin-auth'
import AgentTracePanel from '@/components/messaging/AgentTracePanel.vue'
import ChatMessageContent from '@/components/messaging/ChatMessageContent.vue'

const emit = defineEmits<{ 'unread-change': [count: number] }>()
const auth = useAdminAuthStore()
const conversations = ref<SupportConversation[]>([])
const selected = ref<'ai' | string>('ai')
const workspace = ref<SupportWorkspace | null>(null)
const messages = ref<ChatMessage[]>([])
const aiMessages = ref<ChatMessage[]>([])
const aiPreviousCursor = ref<string | null>(null)
const supportPreviousCursor = ref<string | null>(null)
const loadingEarlier = ref(false)
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const reply = ref('')
const search = ref('')
const userGroupOpen = ref(true)
const storeGroupOpen = ref(true)
const aiConversationId = ref('')
const timeline = ref<HTMLElement | null>(null)
const connectionState = ref<RealtimeState>('polling')
const selectedTraceRunId = ref<string | null>(null)
let realtime: RealtimeConnection | undefined
let pollingTimer: number | undefined
let refreshTimer: number | undefined

const selectedConversation = computed(() => conversations.value.find((item) => item.conversation_id === selected.value) ?? null)
const selectedTicket = computed(() => workspace.value?.ticket ?? null)
const assignedToMe = computed(() => selectedTicket.value?.assigned_user_id === auth.userId)
const canChat = computed(() => assignedToMe.value && selectedTicket.value?.ticket_status === 'active')
const filteredConversations = computed(() => {
  const keyword = search.value.trim().toLocaleLowerCase()
  return keyword ? conversations.value.filter((item) => `${item.participant_name} ${item.participant_id} ${item.last_message_preview || ''}`.toLocaleLowerCase().includes(keyword)) : conversations.value
})
const userConversations = computed(() => filteredConversations.value.filter((item) => item.participant_type === 'user'))
const storeConversations = computed(() => filteredConversations.value.filter((item) => item.participant_type === 'merchant'))
const initials = computed(() => '管')

function token(): string { if (!auth.accessToken) throw new Error('管理会话不可用'); return auth.accessToken }
function dateTime(value: string): string { return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(new Date(value)) }
function traceRunId(message: ChatMessage): string | null {
  const trace = message.content?.execution_trace
  const value = trace && typeof trace === 'object' && !Array.isArray(trace)
    ? (trace as Record<string, unknown>).run_id
    : null
  return typeof value === 'string' ? value : null
}

async function loadConversations(showLoading = false) {
  if (showLoading) loading.value = true
  try {
    conversations.value = (await listSupportConversations({}, token())).data.items
    emit('unread-change', conversations.value.reduce((total, item) => total + item.unread_count, 0))
  } catch (cause) { error.value = errorMessage(cause) }
  finally { if (showLoading) loading.value = false }
}

async function loadAiMessages(replace = true) {
  try {
    const conversation = (await getAdminAiConversation(token())).data
    aiConversationId.value = conversation.conversation_id
    const afterSequence = replace ? 0 : (aiMessages.value.at(-1)?.sequence_no ?? 0)
    const page = (await listAdminAiMessages(token(), afterSequence ? { afterSequence } : {})).data
    if (replace) {
      aiMessages.value = page.items
      aiPreviousCursor.value = page.previous_cursor
    } else {
      appendUnique(aiMessages.value, page.items)
    }
    const latest = aiMessages.value.at(-1)
    if (selected.value === 'ai' && latest && conversation.unread_count) await putAdminAiReadCursor(latest, token())
  } catch (cause) { error.value = errorMessage(cause) }
}

function appendUnique(target: ChatMessage[], incoming: ChatMessage[]) {
  const known = new Set(target.map((item) => item.message_id))
  target.push(...incoming.filter((item) => !known.has(item.message_id)))
}

async function scrollBottom() {
  await nextTick()
  timeline.value?.scrollTo({ top: timeline.value.scrollHeight })
}

async function selectAi() {
  selected.value = 'ai'; workspace.value = null; messages.value = []; supportPreviousCursor.value = null; selectedTraceRunId.value = null; error.value = ''; loading.value = true
  try { await loadAiMessages(); await scrollBottom() } finally { loading.value = false }
}

async function selectConversation(item: SupportConversation) {
  selected.value = item.conversation_id; workspace.value = null; messages.value = []; supportPreviousCursor.value = null; selectedTraceRunId.value = null; error.value = ''; loading.value = true
  try {
    if (item.active_ticket_id) workspace.value = (await getSupportWorkspace(item.active_ticket_id, token())).data
    const page = (await listSupportMessages(item.conversation_id, token())).data
    messages.value = page.items
    supportPreviousCursor.value = page.previous_cursor
    const latest = [...messages.value].reverse().find((message) => message.sender_type === 'user')
    if (latest) await putSupportReadCursor(item.conversation_id, latest, token())
    item.unread_count = 0
    emit('unread-change', conversations.value.reduce((total, conversation) => total + conversation.unread_count, 0))
    await scrollBottom()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function loadEarlier() {
  const cursor = selected.value === 'ai' ? aiPreviousCursor.value : supportPreviousCursor.value
  const current = selectedConversation.value
  const element = timeline.value
  if (!cursor || !element || loadingEarlier.value || (selected.value !== 'ai' && !current)) return
  loadingEarlier.value = true
  const previousHeight = element.scrollHeight
  const previousTop = element.scrollTop
  try {
    const page = selected.value === 'ai'
      ? (await listAdminAiMessages(token(), { cursor })).data
      : (await listSupportMessages(current!.conversation_id, token(), { cursor })).data
    const target = selected.value === 'ai' ? aiMessages.value : messages.value
    const known = new Set(target.map((item) => item.message_id))
    target.unshift(...page.items.filter((item) => !known.has(item.message_id)))
    if (selected.value === 'ai') aiPreviousCursor.value = page.previous_cursor
    else supportPreviousCursor.value = page.previous_cursor
    await nextTick()
    element.scrollTop = previousTop + element.scrollHeight - previousHeight
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loadingEarlier.value = false }
}

async function refreshActiveMessages() {
  try {
    const activeKey = selected.value
    const current = selectedConversation.value
    if (activeKey !== 'ai' && !current) return
    const target = activeKey === 'ai' ? aiMessages.value : messages.value
    const afterSequence = target.at(-1)?.sequence_no ?? 0
    const shouldScroll = !timeline.value || timeline.value.scrollHeight - timeline.value.scrollTop - timeline.value.clientHeight < 90
    const page = activeKey === 'ai'
      ? (await listAdminAiMessages(token(), { afterSequence })).data
      : (await listSupportMessages(current!.conversation_id, token(), { afterSequence })).data
    if (selected.value !== activeKey) return
    appendUnique(target, page.items)
    if (shouldScroll) await scrollBottom()
  } catch (cause) { error.value = errorMessage(cause) }
}

function scheduleRefresh() {
  if (refreshTimer) return
  refreshTimer = window.setTimeout(() => {
    refreshTimer = undefined
    void Promise.all([loadConversations(), refreshActiveMessages()])
  }, 120)
}

function handleRealtime(event: RealtimeEvent) {
  if (['message.created', 'unread.updated', 'support.ticket.updated'].includes(event.type)) scheduleRefresh()
}

async function claim() {
  if (!selectedTicket.value || !selectedConversation.value || busy.value) return
  busy.value = true; error.value = ''
  try {
    const result = await claimSupportTicket(selectedTicket.value, token())
    workspace.value = { ...workspace.value!, ticket: result.data }
    selectedConversation.value.assigned_user_id = result.data.assigned_user_id
    selectedConversation.value.active_ticket_status = result.data.ticket_status
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}

async function send() {
  const text = reply.value.trim()
  if (!text || busy.value) return
  busy.value = true; error.value = ''; reply.value = ''
  try {
    if (selected.value === 'ai') {
      aiMessages.value.push((await sendAdminAiMessage(text, token())).data)
    } else if (selectedConversation.value && selectedTicket.value) {
      const result = await sendSupportMessage(selectedConversation.value.conversation_id, text, token(), createClientMessageId())
      messages.value.push(result.data)
    }
    await scrollBottom()
  } catch (cause) { reply.value = text; error.value = errorMessage(cause) }
  finally { busy.value = false }
}

async function finishHumanService() {
  if (!selectedTicket.value || !canChat.value || busy.value) return
  busy.value = true; error.value = ''
  try {
    await resolveSupportTicket(selectedTicket.value, 'ANSWERED', '本次人工服务已结束，AI 客服恢复接待。', null, token())
    workspace.value = null
    await loadConversations()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}

onMounted(async () => {
  await Promise.all([loadConversations(), loadAiMessages()])
  loading.value = false
  await scrollBottom()
  realtime = new RealtimeConnection({
    audience: 'admin', token, onEvent: handleRealtime,
    onState: (state) => { connectionState.value = state },
    beforeReconnect: () => Promise.all([loadConversations(), refreshActiveMessages()]).then(() => undefined),
  })
  realtime.start()
  pollingTimer = window.setInterval(() => void Promise.all([loadConversations(), refreshActiveMessages()]), 10_000)
})
onBeforeUnmount(() => {
  realtime?.stop()
  if (pollingTimer) window.clearInterval(pollingTimer)
  if (refreshTimer) window.clearTimeout(refreshTimer)
})
</script>

<template>
  <div class="message-page-surface admin-message-page-surface">
    <section class="admin-message-window" aria-label="管理端消息中心">
      <aside class="admin-chat-sidebar">
        <header><div><strong>会话列表</strong><small>用户、店铺与 AI 管家</small></div></header>
        <label class="admin-chat-search"><span>⌕</span><input v-model="search" placeholder="搜索会话" /></label>
        <div class="admin-chat-list">
          <button class="admin-chat-item ai" :class="{ active: selected === 'ai' }" @click="selectAi"><span class="admin-chat-avatar ai">✦</span><span><strong>AI 管家</strong><small>只读诊断助手 · 固定置顶</small></span><time>置顶</time></button>
          <section class="admin-chat-group"><button class="admin-chat-group-title" @click="userGroupOpen = !userGroupOpen"><span>{{ userGroupOpen ? '⌄' : '›' }} 用户消息</span><b>{{ userConversations.length }}</b></button><template v-if="userGroupOpen"><button v-for="item in userConversations" :key="item.conversation_id" class="admin-chat-item" :class="{ active: selected === item.conversation_id }" @click="selectConversation(item)"><span class="admin-chat-avatar">{{ item.participant_name.slice(0, 1) }}</span><span><strong>{{ item.participant_name }}</strong><small>{{ item.requires_human ? '等待人工接待' : 'AI 接待中' }} · {{ item.last_message_preview || '新会话' }}</small></span><b v-if="item.unread_count" :class="{ neutral: !item.requires_human }">{{ item.unread_count }}</b><time>{{ item.last_message_at ? dateTime(item.last_message_at) : '' }}</time></button><p v-if="!userConversations.length">暂无用户会话</p></template></section>
          <section class="admin-chat-group"><button class="admin-chat-group-title" @click="storeGroupOpen = !storeGroupOpen"><span>{{ storeGroupOpen ? '⌄' : '›' }} 店铺消息</span><b>{{ storeConversations.length }}</b></button><template v-if="storeGroupOpen"><button v-for="item in storeConversations" :key="item.conversation_id" class="admin-chat-item" :class="{ active: selected === item.conversation_id }" @click="selectConversation(item)"><span class="admin-chat-avatar store">店</span><span><strong>{{ item.participant_name }}</strong><small>{{ item.requires_human ? '等待平台人工接待' : '商家 AI 助理接待中' }} · {{ item.last_message_preview || '新会话' }}</small></span><b v-if="item.unread_count" :class="{ neutral: !item.requires_human }">{{ item.unread_count }}</b><time>{{ item.last_message_at ? dateTime(item.last_message_at) : '' }}</time></button><p v-if="!storeConversations.length">暂无店铺会话</p></template></section>
        </div>
      </aside>

      <main class="admin-chat-main">
        <template v-if="selected === 'ai'">
          <header class="admin-chat-header"><div><strong>AI 管家</strong><small><span />{{ connectionState === 'connected' ? '实时在线' : connectionState === 'offline' ? '网络离线' : '正在连接' }} · 默认只读</small></div></header>
          <section v-if="!aiMessages.length && !loading" class="admin-ai-concierge">
            <div class="admin-ai-orb">✦</div><p class="eyebrow">管理端智能管家</p><h2>今天想先处理什么？</h2><p>我可以帮助你快速定位用户、店铺、订单和 AI 运行问题。涉及资金、冻结、删除或发布的操作仍会要求人工确认。</p>
            <div class="admin-ai-suggestions"><RouterLink to="/admin/users"><span>♙</span><strong>查看异常用户</strong><small>账号状态与登录会话</small></RouterLink><RouterLink to="/admin/orders"><span>▤</span><strong>检查异常订单</strong><small>支付、履约与售后</small></RouterLink><RouterLink to="/admin/observability"><span>⌇</span><strong>分析 AI 告警</strong><small>延迟、错误与工具调用</small></RouterLink><RouterLink to="/admin/approval-requests"><span>✓</span><strong>查看待办审批</strong><small>高风险操作复核</small></RouterLink></div>
            <div class="alert info">AI 管家已接入受控 Agent Runtime。当前仅开放脱敏、只读诊断，不会修改用户、店铺、订单或资金数据。</div>
          </section>
          <p v-if="error" class="alert error">{{ error }}</p>
          <div v-if="loading" class="admin-chat-loading">正在读取 AI 会话…</div>
          <section v-else class="admin-chat-conversation admin-ai-chat">
            <div v-if="aiMessages.length" ref="timeline" class="admin-chat-timeline"><button v-if="aiPreviousCursor" type="button" class="message-history-button" :disabled="loadingEarlier" @click="loadEarlier">{{ loadingEarlier ? '正在读取更早消息…' : '加载更早消息' }}</button><article v-for="message in aiMessages" :key="message.message_id" :class="{ mine: message.sender_type === 'user', 'trace-selectable': traceRunId(message), 'trace-selected': traceRunId(message) === selectedTraceRunId }" @click="selectedTraceRunId = traceRunId(message) || selectedTraceRunId"><span class="admin-chat-bubble-avatar" :aria-label="message.sender_type === 'user' ? '管理员' : 'AI 管家'">{{ message.sender_type === 'user' ? initials : '✦' }}</span><div><ChatMessageContent :message="message" audience="admin" /><time>{{ dateTime(message.sent_at) }}</time></div></article></div>
            <form class="admin-chat-composer" @submit.prevent="send"><textarea v-model="reply" maxlength="4000" placeholder="询问平台概况、用户、店铺、订单或 Agent 运行状态…" @keydown.enter.exact.prevent="send" /><div><span>默认只读 · 不展示模型原始思维链</span><button :disabled="!reply.trim() || busy">{{ busy ? '发送中…' : '发送' }}</button></div></form>
          </section>
        </template>

        <template v-else>
          <header class="admin-chat-header"><div><strong>{{ selectedConversation?.participant_type === 'merchant' ? '店铺专属客服' : '用户专属客服' }} · {{ selectedConversation?.participant_name }}</strong><small>{{ selectedConversation?.requires_human ? `人工接待 · ${selectedTicket?.ticket_status || '同步中'}` : 'AI 正在接待 · 完整历史已同步' }}</small></div><div class="actions"><button v-if="canChat" class="secondary small" :disabled="busy" @click="finishHumanService">结束人工服务</button><RouterLink v-if="selectedTicket" :to="`/admin/support/tickets/${selectedTicket.ticket_id}`">完整工作台 ↗</RouterLink></div></header>
          <p v-if="error" class="alert error">{{ error }}</p>
          <div v-if="loading" class="admin-chat-loading">正在读取会话…</div>
          <section v-else-if="selectedConversation" class="admin-chat-conversation">
            <div v-if="selectedConversation.requires_human && !assignedToMe" class="admin-chat-claim compact"><span>◍</span><div><h2>{{ selectedTicket?.ticket_status === 'queued' ? '对方正在等待人工接待' : '会话由其他客服处理' }}</h2><p>历史消息可查看；领取后才可人工回复，避免多人同时处理。</p></div><button v-if="selectedTicket?.ticket_status === 'queued' && auth.has('support:claim')" :disabled="busy" @click="claim">领取会话</button></div>
            <div ref="timeline" class="admin-chat-timeline"><button v-if="supportPreviousCursor" type="button" class="message-history-button" :disabled="loadingEarlier" @click="loadEarlier">{{ loadingEarlier ? '正在读取更早消息…' : '加载更早消息' }}</button><article v-for="message in messages" :key="message.message_id" :class="{ mine: message.sender_type === 'human' }"><span class="admin-chat-bubble-avatar" :aria-label="message.sender_type === 'human' ? '平台客服' : message.sender_type === 'agent' ? 'AI 客服' : selectedConversation.participant_type === 'merchant' ? '店铺' : '用户'">{{ message.sender_type === 'human' ? initials : message.sender_type === 'agent' ? '✦' : selectedConversation.participant_type === 'merchant' ? '店' : '用' }}</span><div><ChatMessageContent :message="message" audience="admin" /><time>{{ dateTime(message.sent_at) }}</time></div></article><p v-if="!messages.length" class="empty-state">暂无聊天消息</p></div>
            <form class="admin-chat-composer" @submit.prevent="send"><textarea v-model="reply" maxlength="4000" :disabled="!canChat" :placeholder="canChat ? '输入人工回复…' : selectedConversation.requires_human ? '领取后可回复' : 'AI 正在接待；需要转人工时会进入人工队列'" /><div><span>{{ canChat ? '回复会立即同步给对方，并写入 AI 上下文' : 'AI 与人工消息共用同一条会话历史' }}</span><button :disabled="!canChat || !reply.trim() || busy">发送</button></div></form>
          </section>
        </template>
      </main>
      <AgentTracePanel :messages="selected === 'ai' ? aiMessages : messages" :selected-run-id="selectedTraceRunId" title="AI 运行轨迹" />
    </section>
  </div>
</template>
