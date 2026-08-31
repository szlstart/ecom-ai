<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import {
  claimSupportTicket,
  deleteSupportConversation,
  getSupportWorkspace,
  listSupportConversations,
  listSupportMessages,
  putSupportReadCursor,
  resolveSupportTicket,
  sendSupportProductCard,
  sendSupportMessageResilient,
  type SupportConversation,
  type SupportWorkspace,
} from '@/api/admin-support'
import { errorMessage, messageSendError } from '@/api/http'
import { resolveApiAssetUrl } from '@/api/http'
import { adminGet, requireAdminToken, type AdminProductSummary, type AdminStore } from '@/api/admin-catalog'
import {
  getMerchantExclusiveConversation,
  deleteMerchantExclusiveConversation,
  listMerchantExclusiveMessages,
  putMerchantExclusiveReadCursor,
  sendMerchantExclusiveMessageResilient,
} from '@/api/merchant-support'
import type { ChatMessage } from '@/api/messaging'
import AgentTracePanel from '@/components/messaging/AgentTracePanel.vue'
import ChatMessageContent from '@/components/messaging/ChatMessageContent.vue'
import MessageAttachmentPicker, { type MessagePickerProduct } from '@/components/messaging/MessageAttachmentPicker.vue'
import { liveTraceFromEvent, RealtimeConnection, updateLiveTrace, type AgentLiveTrace, type RealtimeEvent, type RealtimeState } from '@/api/realtime'
import { useAdminAuthStore } from '@/stores/admin-auth'
import { confirmAction } from '@/composables/confirmation'

const props = withDefaults(defineProps<{ storeId?: string; storeName?: string; storeLogoUrl?: string | null; standalone?: boolean }>(), { standalone: false, storeLogoUrl: null })

const auth = useAdminAuthStore()
const route = useRoute()
const loading = ref(false)
const sending = ref(false)
const error = ref('')
const draft = ref('')
const conversations = ref<SupportConversation[]>([])
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
const connectionState = ref<RealtimeState>('polling')
const shaking = ref(false)
const selectedTraceRunId = ref<string | null>(null)
const traceRunning = ref(false)
const liveTrace = ref<AgentLiveTrace | null>(null)
const streamingReply = ref<{ runId: string; text: string; chunkIndex: number } | null>(null)
const attachmentOpen = ref(false)
const attachmentLoading = ref(false)
const attachmentSendingId = ref<string | null>(null)
const attachmentProducts = ref<MessagePickerProduct[]>([])
const currentStore = ref<AdminStore | null>(null)
let realtime: RealtimeConnection | undefined
let pollingTimer: number | undefined
let refreshTimer: number | undefined
let shakeTimer: number | undefined
let initialized = false

const activeConversation = computed(() => conversations.value.find((item) => item.conversation_id === selectedKey.value) ?? null)
const activeTicket = computed(() => workspace.value?.ticket ?? null)
const activeMessages = computed(() => selectedKey.value === 'exclusive' ? exclusiveMessages.value : messages.value)
const unreadCount = computed(() => exclusiveUnread.value + conversations.value.reduce((total, item) => total + item.unread_count, 0))
const humanUnreadCount = computed(() => conversations.value.reduce((total, item) => total + (item.requires_human ? item.unread_count : 0), 0))
const title = computed(() => selectedKey.value === 'exclusive' ? '专属客服' : activeConversation.value?.participant_name || '顾客咨询')
const canReply = computed(() => activeTicket.value?.ticket_status === 'active' && activeTicket.value.assigned_user_id === auth.userId)
const subtitle = computed(() => {
  if (selectedKey.value === 'exclusive') return 'AI 经营助理 · 默认只读'
  if (!activeConversation.value?.requires_human) return 'AI 正在接待 · 对话已同步给你'
  return activeTicket.value ? statusLabel(activeTicket.value.ticket_status) : '正在同步人工接待状态'
})
const resolvedStoreName = computed(() => props.storeName || currentStore.value?.store_name || '店铺')
const resolvedStoreLogo = computed(() => props.storeLogoUrl || currentStore.value?.logo_url || null)

function token() { return auth.accessToken! }
function statusLabel(value: string) {
  return ({ queued: '等待接待', assigned: '已分配', active: '正在沟通', waiting_user: '等待顾客', resolved: '已解决', closed: '已关闭' } as Record<string, string>)[value] ?? value
}
function isRight(item: ChatMessage) {
  return selectedKey.value === 'exclusive'
    ? item.sender_type === 'user'
    : ['agent', 'human'].includes(item.sender_type)
}
function avatarLabel(item: ChatMessage): string {
  if (item.sender_type === 'agent') return '✦'
  if (selectedKey.value === 'exclusive' && item.sender_type === 'human') return '管'
  if (item.sender_type === 'human' || (selectedKey.value === 'exclusive' && item.sender_type === 'user')) return resolvedStoreName.value.slice(0, 1) || '店'
  return '客'
}
function avatarUrl(item: ChatMessage): string | null {
  if (item.sender_type === 'agent') return '/ai-avatar.svg'
  if (selectedKey.value === 'exclusive' && item.sender_type === 'human') return null
  if (selectedKey.value !== 'exclusive' && item.sender_type === 'user') {
    return resolveApiAssetUrl(activeConversation.value?.participant_avatar_url ?? null)
  }
  return item.sender_type === 'human' || (selectedKey.value === 'exclusive' && item.sender_type === 'user')
    ? resolveApiAssetUrl(resolvedStoreLogo.value)
    : null
}
async function removeConversation() {
  if (sending.value) return
  const name = selectedKey.value === 'exclusive' ? '专属客服' : activeConversation.value?.participant_name || '当前顾客'
  if (!await confirmAction(`确认删除与“${name}”的对话吗？聊天记录和本会话 AI 记忆会被清除，且无法恢复。`)) return
  sending.value = true; error.value = ''
  try {
    if (selectedKey.value === 'exclusive') {
      await deleteMerchantExclusiveConversation(token())
      exclusiveMessages.value = []
      exclusiveConversationId.value = ''
      await loadExclusive()
    } else if (activeConversation.value) {
      const deletedId = activeConversation.value.conversation_id
      await deleteSupportConversation(deletedId, token())
      conversations.value = conversations.value.filter((item) => item.conversation_id !== deletedId)
      await selectConversation('exclusive')
    }
  } catch (cause) { error.value = errorMessage(cause) }
  finally { sending.value = false }
}
function traceRunId(item: ChatMessage): string | null {
  const trace = item.content?.execution_trace
  const value = trace && typeof trace === 'object' && !Array.isArray(trace)
    ? (trace as Record<string, unknown>).run_id
    : null
  return typeof value === 'string' ? value : null
}
async function scrollBottom() {
  await nextTick()
  if (timeline.value) timeline.value.scrollTop = timeline.value.scrollHeight
}

async function markActiveRead(lastMessage: ChatMessage | undefined) {
  if (!lastMessage || !props.standalone || document.visibilityState !== 'visible') return
  if (selectedKey.value === 'exclusive') {
    exclusiveUnread.value = (await putMerchantExclusiveReadCursor(lastMessage, token())).data.unread_count
    return
  }
  const conversationId = selectedKey.value
  const result = await putSupportReadCursor(conversationId, lastMessage, token())
  const current = conversations.value.find((item) => item.conversation_id === conversationId)
  if (current) current.unread_count = result.data.unread_count
}

async function loadConversations() {
  const previousUnread = unreadCount.value
  try {
    conversations.value = (await listSupportConversations({}, token())).data.items
    if (initialized && unreadCount.value > previousUnread) shake()
    error.value = ''
  }
  catch (cause) { error.value = errorMessage(cause) }
}

async function loadExclusive(markRead = props.standalone && selectedKey.value === 'exclusive') {
  loading.value = true; error.value = ''
  let loaded = false
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
    if (markRead && exclusiveUnread.value) await markActiveRead(lastMessage)
    error.value = ''
    loaded = true
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
  if (loaded) await scrollBottom()
}

async function selectConversation(key: string) {
  selectedKey.value = key; workspace.value = null; messages.value = []; supportPreviousCursor.value = null; selectedTraceRunId.value = null; traceRunning.value = false; liveTrace.value = null; streamingReply.value = null; attachmentOpen.value = false; error.value = ''
  if (key === 'exclusive') { await loadExclusive(); return }
  const target = conversations.value.find((item) => item.conversation_id === key)
  if (!target) return
  loading.value = true
  let loaded = false
  try {
    if (target.active_ticket_id) {
      workspace.value = (await getSupportWorkspace(target.active_ticket_id, token())).data
      if (workspace.value.ticket.ticket_status === 'queued') {
        workspace.value.ticket = (await claimSupportTicket(workspace.value.ticket, token())).data
        target.active_ticket_status = workspace.value.ticket.ticket_status
        target.assigned_user_id = workspace.value.ticket.assigned_user_id
      }
    }
    const messageResult = await listSupportMessages(target.conversation_id, token())
    messages.value = messageResult.data.items
    supportPreviousCursor.value = messageResult.data.previous_cursor
    const lastMessage = messages.value.at(-1)
    if (target.unread_count) await markActiveRead(lastMessage)
    error.value = ''
    loaded = true
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
  if (loaded) await scrollBottom()
}

async function loadEarlier() {
  const cursor = selectedKey.value === 'exclusive' ? exclusivePreviousCursor.value : supportPreviousCursor.value
  const active = activeConversation.value
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
    const active = activeConversation.value
    if (activeKey !== 'exclusive' && !active) return
    const shouldScroll = !timeline.value || timeline.value.scrollHeight - timeline.value.scrollTop - timeline.value.clientHeight < 90
    const page = activeKey === 'exclusive'
      ? (await listMerchantExclusiveMessages(token(), { afterSequence })).data
      : (await listSupportMessages(active!.conversation_id, token(), { afterSequence })).data
    if (selectedKey.value !== activeKey) return
    const known = new Set(target.value.map((item) => item.message_id))
    const incoming = page.items.filter((item) => !known.has(item.message_id))
    target.value = [...target.value, ...incoming]
    error.value = ''
    if (shouldScroll) await scrollBottom()
    if (incoming.length) await markActiveRead(target.value.at(-1))
  } catch (cause) { error.value = errorMessage(cause) }
}

function shake() {
  if (route.path === '/merchant/messages') return
  shaking.value = false
  window.requestAnimationFrame(() => { shaking.value = true })
  if (shakeTimer) window.clearTimeout(shakeTimer)
  shakeTimer = window.setTimeout(() => { shaking.value = false }, 700)
}

function scheduleRefresh() {
  if (refreshTimer) return
  refreshTimer = window.setTimeout(() => {
    refreshTimer = undefined
    void Promise.all([loadConversations(), refreshActiveMessages()])
  }, 120)
}

function handleRealtime(event: RealtimeEvent) {
  const eventConversationId = String(event.data.conversation_id ?? '')
  const selectedConversationId = selectedKey.value === 'exclusive' ? exclusiveConversationId.value : selectedKey.value
  if (eventConversationId === selectedConversationId && event.type === 'agent.response.started') {
    traceRunning.value = true
    liveTrace.value = liveTraceFromEvent(event)
    const runId = event.data.run_id
    if (typeof runId === 'string') {
      streamingReply.value = { runId, text: '', chunkIndex: 0 }
      selectedTraceRunId.value = runId
    }
  }
  if (eventConversationId === selectedConversationId && event.type === 'agent.response.reasoning.delta') { traceRunning.value = true; liveTrace.value = updateLiveTrace(liveTrace.value, event) }
  if (eventConversationId === selectedConversationId && event.type === 'agent.response.delta') {
    const runId = event.data.run_id
    const text = event.data.text_so_far
    const chunkIndex = Number(event.data.chunk_index)
    if (typeof runId === 'string' && typeof text === 'string' && Number.isInteger(chunkIndex)) {
      if (!streamingReply.value || streamingReply.value.runId !== runId) streamingReply.value = { runId, text: '', chunkIndex: 0 }
      if (chunkIndex > streamingReply.value.chunkIndex) streamingReply.value = { runId, text, chunkIndex }
      void scrollBottom()
    }
  }
  if (eventConversationId === selectedConversationId && event.type === 'agent.response.completed') { traceRunning.value = false; liveTrace.value = null; streamingReply.value = null }
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
      const sent = (await sendMerchantExclusiveMessageResilient(text, token())).data
      exclusiveMessages.value.push(sent)
    } else if (activeConversation.value && canReply.value) {
      messages.value.push((await sendSupportMessageResilient(activeConversation.value.conversation_id, text, token())).data)
    }
    draft.value = ''
    await scrollBottom()
  } catch (cause) { error.value = messageSendError(cause) }
  finally { sending.value = false }
}

async function finishHumanService() {
  if (!activeTicket.value || !canReply.value || sending.value) return
  sending.value = true; error.value = ''
  try {
    await resolveSupportTicket(activeTicket.value, 'ANSWERED', '本次人工服务已结束，AI 客服恢复接待。', null, token())
    workspace.value = null
    await Promise.all([loadConversations(), refreshActiveMessages()])
  } catch (cause) { error.value = errorMessage(cause) }
  finally { sending.value = false }
}

async function openAttachments() {
  if (selectedKey.value === 'exclusive' || !canReply.value || attachmentLoading.value) return
  attachmentOpen.value = true
  attachmentLoading.value = true
  error.value = ''
  try {
    const result = await adminGet<{ items: AdminProductSummary[]; next_cursor: string | null }>('/admin/products?status=on_sale&limit=100', requireAdminToken(auth.accessToken))
    attachmentProducts.value = result.data.items.map((item) => ({
      product_id: item.product_id,
      product_name: item.product_name,
      image_url: resolveApiAssetUrl(item.cover_image_url),
      price_label: `¥${item.min_price}${item.min_price === item.max_price ? '' : ' 起'}`,
      sku_id: null,
      meta: `库存 ${item.available_quantity} · 已售 ${item.sales_count}`,
    }))
  } catch (cause) { error.value = errorMessage(cause); attachmentOpen.value = false }
  finally { attachmentLoading.value = false }
}
async function sendPickedProduct(item: MessagePickerProduct) {
  if (!activeConversation.value || !canReply.value || attachmentSendingId.value) return
  attachmentSendingId.value = item.product_id
  try {
    messages.value.push((await sendSupportProductCard(activeConversation.value.conversation_id, item.product_id, item.sku_id, token())).data)
    attachmentOpen.value = false
    await scrollBottom()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { attachmentSendingId.value = null }
}

onMounted(async () => {
  const storeRequest = props.standalone
    ? adminGet<{ items: AdminStore[]; next_cursor: string | null }>('/admin/stores?limit=1', requireAdminToken(auth.accessToken))
      .then((result) => { currentStore.value = result.data.items[0] ?? null })
      .catch(() => undefined)
    : Promise.resolve()
  await Promise.all([loadConversations(), loadExclusive(props.standalone), storeRequest])
  initialized = true
  realtime = new RealtimeConnection({
    audience: 'admin', token, onEvent: handleRealtime,
    onState: (state) => { connectionState.value = state },
    beforeReconnect: () => Promise.all([loadConversations(), refreshActiveMessages()]).then(() => undefined),
  })
  realtime.start()
  pollingTimer = window.setInterval(() => void Promise.all([loadConversations(), refreshActiveMessages()]), 10_000)
})
onBeforeUnmount(() => {
  traceRunning.value = false
  realtime?.stop()
  if (pollingTimer) window.clearInterval(pollingTimer)
  if (refreshTimer) window.clearTimeout(refreshTimer)
  if (shakeTimer) window.clearTimeout(shakeTimer)
})
</script>

<template>
  <RouterLink v-if="!standalone" class="merchant-message-trigger" :class="{ 'message-arrival-shake': shaking }" to="/merchant/messages">
    <span aria-hidden="true">💬</span><span>消息</span><b v-if="unreadCount" :class="{ neutral: !humanUnreadCount }" :aria-label="`${unreadCount} 条未读消息`">{{ unreadCount > 99 ? '99+' : unreadCount }}</b>
  </RouterLink>
  <div v-else class="message-page-surface merchant-message-page-surface">
      <section class="merchant-message-window" aria-label="商家消息中心">
        <aside class="merchant-chat-list">
          <header><div><strong>会话列表</strong><small><span class="connection-dot" :class="connectionState" />{{ unreadCount ? `${unreadCount} 条未读` : '消息已读' }}</small></div><RouterLink class="message-workspace-back" to="/merchant/products">返回</RouterLink></header>
          <button class="merchant-chat-item pinned" :class="{ active: selectedKey === 'exclusive' }" type="button" @click="selectConversation('exclusive')">
            <span class="merchant-chat-avatar platform"><img src="/ai-avatar.svg" alt="" /></span><span><strong>专属客服 <em>置顶</em></strong><small>面向商家的 AI 经营助理</small></span><i v-if="exclusiveUnread" class="merchant-chat-unread">{{ exclusiveUnread > 99 ? '99+' : exclusiveUnread }}</i>
          </button>
          <p v-if="!conversations.length" class="merchant-chat-empty">暂时没有顾客咨询</p>
          <button v-for="item in conversations" :key="item.conversation_id" class="merchant-chat-item" :class="{ active: selectedKey === item.conversation_id }" type="button" @click="selectConversation(item.conversation_id)">
            <span class="merchant-chat-avatar"><img v-if="item.participant_avatar_url" :src="resolveApiAssetUrl(item.participant_avatar_url) || undefined" alt="" /><template v-else>{{ item.participant_name.slice(0, 1) || '客' }}</template></span><span><strong>{{ item.participant_name }}</strong><small>{{ item.requires_human ? statusLabel(item.active_ticket_status || '') : 'AI 接待中' }} · {{ item.last_message_preview || '新会话' }}</small></span><i v-if="item.unread_count" class="merchant-chat-unread" :class="{ neutral: !item.requires_human }">{{ item.unread_count > 99 ? '99+' : item.unread_count }}</i>
          </button>
        </aside>
        <main class="merchant-chat-main">
          <header><div><strong>{{ title }}</strong><small>{{ subtitle }}</small></div><div class="actions"><button v-if="canReply" class="secondary small" :disabled="sending" @click="finishHumanService">结束人工服务</button><button class="danger small" type="button" :disabled="sending" @click="removeConversation">删除对话</button></div></header>
          <p v-if="error" class="merchant-chat-error">{{ error }}</p>
          <div ref="timeline" class="merchant-chat-timeline">
            <button v-if="selectedKey === 'exclusive' ? exclusivePreviousCursor : supportPreviousCursor" type="button" class="message-history-button" :disabled="loadingEarlier" @click="loadEarlier">{{ loadingEarlier ? '正在读取更早消息…' : '加载更早消息' }}</button>
            <div v-if="selectedKey === 'exclusive' && !activeMessages.length && !loading" class="merchant-chat-welcome"><span class="merchant-chat-avatar platform"><img src="/ai-avatar.svg" alt="" /></span><h2>你好，我是你的专属客服</h2><p>我可以在当前店铺范围内分析商品、订单、库存和经营概况。默认只读，不会替你修改业务数据。</p></div>
            <p v-if="loading" class="merchant-chat-loading">正在读取消息…</p>
            <article v-for="item in activeMessages" :key="item.message_id" class="merchant-chat-bubble-row" :class="{ mine: isRight(item), system: item.sender_type === 'system', 'trace-selectable': traceRunId(item), 'trace-selected': traceRunId(item) === selectedTraceRunId }" @click="selectedTraceRunId = traceRunId(item) || selectedTraceRunId"><span v-if="item.sender_type !== 'system'" class="merchant-chat-avatar" :class="{ platform: item.sender_type === 'agent' }"><img v-if="avatarUrl(item)" :src="avatarUrl(item)!" alt="" />{{ avatarUrl(item) ? '' : avatarLabel(item) }}</span><div class="merchant-chat-bubble"><ChatMessageContent :message="item" audience="merchant" /><time v-if="item.sender_type !== 'system'">{{ new Date(item.sent_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }}</time></div></article>
            <article v-if="streamingReply" class="merchant-chat-bubble-row" :class="{ mine: selectedKey !== 'exclusive' }"><span class="merchant-chat-avatar platform"><img src="/ai-avatar.svg" alt="" /></span><div class="merchant-chat-bubble agent-stream" aria-live="polite"><p v-if="streamingReply.text">{{ streamingReply.text }}</p><p v-else class="agent-thinking-indicator">正在思考<span>·</span><span>·</span><span>·</span></p><time>正在生成回复…</time></div></article>
          </div>
          <form class="merchant-chat-composer unified-chat-composer" :class="{ 'without-attachments': selectedKey === 'exclusive' }" @submit.prevent="send"><button v-if="selectedKey !== 'exclusive'" type="button" class="message-plus-button" :disabled="!canReply" aria-label="发送本店商品" title="发送本店商品" @click="openAttachments">＋</button><textarea v-model="draft" rows="3" maxlength="4000" :disabled="selectedKey !== 'exclusive' && !canReply" :placeholder="selectedKey === 'exclusive' ? '向专属客服描述经营问题…' : canReply ? '回复顾客…' : 'AI 正在接待；转人工后可在这里回复'" @keydown.enter.exact.prevent="send" /><button class="unified-chat-send" :disabled="sending || !draft.trim() || (selectedKey !== 'exclusive' && !canReply)">{{ sending ? '发送中…' : '发送' }}</button><small>{{ selectedKey === 'exclusive' || canReply ? 'Enter 发送 · Shift + Enter 换行' : '输入区始终保留；AI 转人工后即可回复' }}</small></form>
        </main>
        <AgentTracePanel :messages="activeMessages" :selected-run-id="selectedTraceRunId" :running="traceRunning" :live-trace="liveTrace" title="思考过程" />
      </section>
      <MessageAttachmentPicker :open="attachmentOpen" :loading="attachmentLoading" :products="attachmentProducts" :sending-id="attachmentSendingId" title="发送本店商品" @close="attachmentOpen = false" @product="sendPickedProduct" />
  </div>
</template>
