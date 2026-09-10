<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import {
  clearAdminAiConversationHistory,
  clearSupportConversationHistory,
  claimSupportTicket,
  deleteAdminAiConversation,
  deleteSupportConversation,
  getAdminAiConversation,
  getSupportWorkspace,
  listAdminAiMessages,
  listSupportConversations,
  listSupportMessages,
  putAdminAiReadCursor,
  putSupportReadCursor,
  resolveSupportTicket,
  sendSupportOrderCard,
  sendSupportProductCard,
  sendAdminAiMessageResilient,
  sendSupportMessageResilient,
  type SupportConversation,
  type SupportWorkspace,
} from '@/api/admin-support'
import { errorMessage, messageSendError, resolveApiAssetUrl } from '@/api/http'
import { createClientMessageId, type ChatMessage } from '@/api/messaging'
import { liveTraceFromEvent, RealtimeConnection, updateLiveTrace, type AgentLiveTrace, type RealtimeEvent, type RealtimeState } from '@/api/realtime'
import { useAdminAuthStore } from '@/stores/admin-auth'
import AgentTracePanel from '@/components/messaging/AgentTracePanel.vue'
import ChatMessageContent from '@/components/messaging/ChatMessageContent.vue'
import MessageAttachmentPicker, { type MessagePickerOrder, type MessagePickerProduct } from '@/components/messaging/MessageAttachmentPicker.vue'
import { confirmAction } from '@/composables/confirmation'
import { adminGet, requireAdminToken, type AdminProductSummary } from '@/api/admin-catalog'
import { listAdminOrders, type AdminOrderSummary } from '@/api/orders'

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
const traceRunning = ref(false)
const liveTrace = ref<AgentLiveTrace | null>(null)
const openMenuKey = ref('')
const streamingReply = ref<{ runId: string; text: string; chunkIndex: number } | null>(null)
const attachmentOpen = ref(false)
const attachmentLoading = ref(false)
const attachmentSendingId = ref<string | null>(null)
const attachmentProducts = ref<MessagePickerProduct[]>([])
const attachmentOrders = ref<MessagePickerOrder[]>([])
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
const userUnread = computed(() => userConversations.value.reduce((total, item) => total + item.unread_count, 0))
const storeUnread = computed(() => storeConversations.value.reduce((total, item) => total + item.unread_count, 0))
const aiPrompts = ['生成平台运营简报', '检查用户、店铺和订单异常', '检查待处理交易与售后风险', '检查 Agent、模型和任务运行状态', '给出今日治理优先级']
const userReplyTemplates = ['您好，我已接入本次平台服务，请告诉我需要重点处理的问题。', '我正在核对您的订单与处理记录，请稍等。', '为了继续处理，请补充相关商品、订单或售后信息。']
const merchantReplyTemplates = ['您好，我已接入本次商家平台服务，请说明需要平台协助的事项。', '我正在核对店铺、商品和订单状态，请稍等。', '为了继续处理，请补充涉及的商品、订单或审核信息。']

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
    error.value = ''
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
    error.value = ''
  } catch (cause) { error.value = errorMessage(cause) }
}

function appendUnique(target: ChatMessage[], incoming: ChatMessage[]) {
  const known = new Set(target.map((item) => item.message_id))
  target.push(...incoming.filter((item) => !known.has(item.message_id)))
}

async function scrollBottom() {
  await nextTick()
  if (timeline.value) timeline.value.scrollTop = timeline.value.scrollHeight
}

async function selectAi() {
  selected.value = 'ai'; workspace.value = null; messages.value = []; supportPreviousCursor.value = null; selectedTraceRunId.value = null; traceRunning.value = false; liveTrace.value = null; streamingReply.value = null; error.value = ''; loading.value = true
  try { await loadAiMessages() } finally { loading.value = false }
  await scrollBottom()
}

async function selectConversation(item: SupportConversation) {
  selected.value = item.conversation_id; workspace.value = null; messages.value = []; supportPreviousCursor.value = null; selectedTraceRunId.value = null; traceRunning.value = false; liveTrace.value = null; streamingReply.value = null; attachmentOpen.value = false; error.value = ''; loading.value = true
  let loaded = false
  try {
    if (item.active_ticket_id) workspace.value = (await getSupportWorkspace(item.active_ticket_id, token())).data
    const page = (await listSupportMessages(item.conversation_id, token())).data
    messages.value = page.items
    supportPreviousCursor.value = page.previous_cursor
    const latest = [...messages.value].reverse().find((message) => message.sender_type === 'user')
    if (latest) await putSupportReadCursor(item.conversation_id, latest, token())
    item.unread_count = 0
    emit('unread-change', conversations.value.reduce((total, conversation) => total + conversation.unread_count, 0))
    error.value = ''
    loaded = true
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
  if (loaded) await scrollBottom()
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
    error.value = ''
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
  const eventConversationId = String(event.data.conversation_id ?? '')
  const selectedConversationId = selected.value === 'ai' ? aiConversationId.value : selected.value
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
  if (eventConversationId === selectedConversationId && event.type === 'agent.response.completed') { traceRunning.value = false }
  if (event.type === 'message.created') {
    const message = event.data.message as { content?: { run_id?: string } } | undefined
    if (message?.content?.run_id && message.content.run_id === streamingReply.value?.runId) {
      streamingReply.value = null
      liveTrace.value = null
    }
  }
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
      aiMessages.value.push((await sendAdminAiMessageResilient(text, token())).data)
    } else if (selectedConversation.value && selectedTicket.value) {
      const result = await sendSupportMessageResilient(selectedConversation.value.conversation_id, text, token(), createClientMessageId())
      messages.value.push(result.data)
    }
    await scrollBottom()
  } catch (cause) { reply.value = text; error.value = messageSendError(cause) }
  finally { busy.value = false }
}

async function useQuickText(text: string, sendImmediately: boolean) {
  if (busy.value) return
  reply.value = text
  if (sendImmediately) await send()
}

function orderPickerItem(item: AdminOrderSummary): MessagePickerOrder {
  const order = item.order
  const firstItem = order.items[0]
  const minorUnits = Number(order.amounts.payable_amount.minor_units)
  return {
    order_id: order.order_id,
    title: firstItem?.product_name || '用户订单',
    image_url: resolveApiAssetUrl(firstItem?.image_url ?? null),
    amount_label: Number.isSafeInteger(minorUnits) ? `¥${(minorUnits / 100).toFixed(2)}` : '金额待确认',
    status_label: ({ pending_payment: '待付款', paid: '已付款', pending_shipment: '待发货', shipped: '运输中', completed: '已完成', closed: '已关闭' } as Record<string, string>)[order.order_status] || '状态更新中',
  }
}

async function openAttachments() {
  const conversation = selectedConversation.value
  if (!conversation || conversation.participant_type !== 'user' || !canChat.value || attachmentLoading.value) return
  attachmentOpen.value = true
  attachmentLoading.value = true
  error.value = ''
  try {
    const productQuery = new URLSearchParams({ status: 'on_sale', limit: '100' })
    if (conversation.store_id) productQuery.set('store_id', conversation.store_id)
    const [productResult, orderResult] = await Promise.all([
      adminGet<{ items: AdminProductSummary[]; next_cursor: string | null }>(`/admin/products?${productQuery}`, requireAdminToken(auth.accessToken)),
      listAdminOrders({ q: conversation.participant_id, ...(conversation.store_id ? { store_id: conversation.store_id } : {}) }, token()),
    ])
    attachmentProducts.value = productResult.data.items.map((item) => ({
      product_id: item.product_id,
      product_name: item.product_name,
      image_url: resolveApiAssetUrl(item.cover_image_url),
      price_label: `¥${item.min_price}${item.min_price === item.max_price ? '' : ' 起'}`,
      sku_id: null,
      meta: `${item.store_name} · 库存 ${item.available_quantity}`,
    }))
    attachmentOrders.value = orderResult.data.items.map(orderPickerItem)
  } catch (cause) { error.value = errorMessage(cause); attachmentOpen.value = false }
  finally { attachmentLoading.value = false }
}

async function sendPickedProduct(item: MessagePickerProduct) {
  if (!selectedConversation.value || !canChat.value || attachmentSendingId.value) return
  attachmentSendingId.value = item.product_id
  try {
    messages.value.push((await sendSupportProductCard(selectedConversation.value.conversation_id, item.product_id, item.sku_id, token())).data)
    attachmentOpen.value = false
    await scrollBottom()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { attachmentSendingId.value = null }
}

async function sendPickedOrder(item: MessagePickerOrder) {
  if (!selectedConversation.value || !canChat.value || attachmentSendingId.value) return
  attachmentSendingId.value = item.order_id
  try {
    messages.value.push((await sendSupportOrderCard(selectedConversation.value.conversation_id, item.order_id, token())).data)
    attachmentOpen.value = false
    await scrollBottom()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { attachmentSendingId.value = null }
}

function businessContextLabel(type: string): string {
  return ({ product: '商品', order: '订单', shipment: '物流', refund: '售后', store: '店铺', checkout_store_group: '结算' } as Record<string, string>)[type] || '业务对象'
}

function businessContextRoute(type: string, id: string): string | null {
  if (type === 'product') return `/admin/products/${encodeURIComponent(id)}`
  if (type === 'order') return `/admin/orders/${encodeURIComponent(id)}`
  if (type === 'refund') return `/admin/refund-applications/${encodeURIComponent(id)}`
  if (type === 'store') return `/admin/stores/${encodeURIComponent(id)}`
  return null
}

async function finishHumanService() {
  if (!selectedTicket.value || !canChat.value || busy.value) return
  busy.value = true; error.value = ''
  try {
    await resolveSupportTicket(selectedTicket.value, 'ANSWERED', '本次人工服务已结束，AI 客服恢复接待。', null, token())
    workspace.value = null
    await Promise.all([loadConversations(), refreshActiveMessages()])
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}

async function clearHistory() {
  if (busy.value) return
  const name = selected.value === 'ai' ? 'AI 管家' : selectedConversation.value?.participant_name || '当前会话'
  if (!await confirmAction(`确认清除与“${name}”的聊天记录吗？会话仍保留在左侧，历史与本会话 AI 记忆会清空，并重新开始。`)) return
  busy.value = true; error.value = ''
  try {
    if (selected.value === 'ai') {
      await clearAdminAiConversationHistory(token())
      aiMessages.value = []
      await loadAiMessages()
    } else if (selectedConversation.value) {
      await clearSupportConversationHistory(selectedConversation.value.conversation_id, token())
      messages.value = []
      supportPreviousCursor.value = null
      workspace.value = null
      await loadConversations()
    }
    selectedTraceRunId.value = null
    liveTrace.value = null
    streamingReply.value = null
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}

async function deleteConversationEntry(key: string, name: string) {
  openMenuKey.value = ''
  if (busy.value || !await confirmAction(`确认删除与“${name}”的对话吗？聊天记录、上下文和本会话 AI 记忆都会清除，且无法恢复。`)) return
  busy.value = true; error.value = ''
  try {
    if (key === 'ai') {
      await deleteAdminAiConversation(token())
      aiMessages.value = []
      aiConversationId.value = ''
      await loadAiMessages()
    } else {
      await deleteSupportConversation(key, token())
      conversations.value = conversations.value.filter((item) => item.conversation_id !== key)
      if (selected.value === key) await selectAi()
    }
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
  traceRunning.value = false
  realtime?.stop()
  if (pollingTimer) window.clearInterval(pollingTimer)
  if (refreshTimer) window.clearTimeout(refreshTimer)
})
</script>

<template>
  <div class="message-page-surface admin-message-page-surface">
    <section class="admin-message-window" aria-label="管理端消息中心" @click="openMenuKey = ''" @keydown.esc="openMenuKey = ''">
      <aside class="admin-chat-sidebar">
        <header><div><strong>会话列表</strong><small>用户、店铺与 AI 管家</small></div><RouterLink class="message-workspace-back" to="/admin">返回</RouterLink></header>
        <label class="admin-chat-search"><span>⌕</span><input v-model="search" placeholder="搜索会话" /></label>
        <div class="admin-chat-list">
          <div class="message-conversation-entry" @contextmenu.prevent.stop="openMenuKey = 'ai'"><button class="admin-chat-item ai" :class="{ active: selected === 'ai' }" @click.stop="selectAi"><span class="admin-chat-avatar ai"><img src="/ai-avatar.svg" alt="" /></span><span><strong>AI 管家</strong><small>只读诊断助手 · 固定置顶</small></span><time>置顶</time></button><div v-if="openMenuKey === 'ai'" class="message-conversation-menu" role="menu" @click.stop><button type="button" role="menuitem" @click="deleteConversationEntry('ai', 'AI 管家')">删除对话</button></div></div>
          <section class="admin-chat-group"><button class="admin-chat-group-title" @click="userGroupOpen = !userGroupOpen"><span>{{ userGroupOpen ? '⌄' : '›' }} 用户消息</span><b>{{ userConversations.length }}<i v-if="userUnread">{{ userUnread > 99 ? '99+' : userUnread }}</i></b></button><template v-if="userGroupOpen"><div v-for="item in userConversations" :key="item.conversation_id" class="message-conversation-entry" @contextmenu.prevent.stop="openMenuKey = item.conversation_id"><button class="admin-chat-item" :class="{ active: selected === item.conversation_id }" @click.stop="selectConversation(item)"><span class="admin-chat-avatar"><img v-if="item.participant_avatar_url" :src="resolveApiAssetUrl(item.participant_avatar_url) || undefined" alt="" /><template v-else>{{ item.participant_name.slice(0, 1) }}</template></span><span><strong>{{ item.participant_name }}</strong><small>{{ item.requires_human ? '等待人工接待' : 'AI 接待中' }} · {{ item.last_message_preview || '新会话' }}</small></span><b v-if="item.unread_count" :class="{ neutral: !item.requires_human }">{{ item.unread_count }}</b><time>{{ item.last_message_at ? dateTime(item.last_message_at) : '' }}</time></button><div v-if="openMenuKey === item.conversation_id" class="message-conversation-menu" role="menu" @click.stop><button type="button" role="menuitem" @click="deleteConversationEntry(item.conversation_id, item.participant_name)">删除对话</button></div></div><p v-if="!userConversations.length">暂无用户会话</p></template></section>
          <section class="admin-chat-group"><button class="admin-chat-group-title" @click="storeGroupOpen = !storeGroupOpen"><span>{{ storeGroupOpen ? '⌄' : '›' }} 店铺消息</span><b>{{ storeConversations.length }}<i v-if="storeUnread">{{ storeUnread > 99 ? '99+' : storeUnread }}</i></b></button><template v-if="storeGroupOpen"><div v-for="item in storeConversations" :key="item.conversation_id" class="message-conversation-entry" @contextmenu.prevent.stop="openMenuKey = item.conversation_id"><button class="admin-chat-item" :class="{ active: selected === item.conversation_id }" @click.stop="selectConversation(item)"><span class="admin-chat-avatar store"><img v-if="item.participant_avatar_url" :src="resolveApiAssetUrl(item.participant_avatar_url) || undefined" alt="" /><template v-else>{{ item.participant_name.slice(0, 1) || '店' }}</template></span><span><strong>{{ item.participant_name }}</strong><small>{{ item.requires_human ? '等待平台人工接待' : 'AI 经营助理接待中' }} · {{ item.last_message_preview || '新会话' }}</small></span><b v-if="item.unread_count" :class="{ neutral: !item.requires_human }">{{ item.unread_count }}</b><time>{{ item.last_message_at ? dateTime(item.last_message_at) : '' }}</time></button><div v-if="openMenuKey === item.conversation_id" class="message-conversation-menu" role="menu" @click.stop><button type="button" role="menuitem" @click="deleteConversationEntry(item.conversation_id, item.participant_name)">删除对话</button></div></div><p v-if="!storeConversations.length">暂无店铺会话</p></template></section>
        </div>
      </aside>

      <main class="admin-chat-main">
        <template v-if="selected === 'ai'">
          <header class="admin-chat-header"><div><strong>AI 管家</strong><small><span />{{ connectionState === 'connected' ? '实时在线' : connectionState === 'offline' ? '网络离线' : '正在连接' }} · 默认只读</small></div><button class="secondary small" type="button" :disabled="busy" @click="clearHistory">清除记录</button></header>
          <p v-if="error" class="alert error">{{ error }}</p>
          <div v-if="loading" class="admin-chat-loading">正在读取 AI 会话…</div>
          <section v-else class="admin-chat-conversation admin-ai-chat">
            <div ref="timeline" class="admin-chat-timeline"><button v-if="aiPreviousCursor" type="button" class="message-history-button" :disabled="loadingEarlier" @click="loadEarlier">{{ loadingEarlier ? '正在读取更早消息…' : '加载更早消息' }}</button><article v-if="!aiMessages.length" class="admin-ai-welcome"><span class="admin-chat-bubble-avatar"><img src="/ai-avatar.svg" alt="" /></span><div><p>你好，我是 AI 管家。用户、店铺、商品、订单和运行状态都可以问我。今天想先查看哪一部分？</p></div></article><article v-for="message in aiMessages" :key="message.message_id" :class="{ mine: message.sender_type === 'user', 'trace-selectable': traceRunId(message), 'trace-selected': traceRunId(message) === selectedTraceRunId }" @click="selectedTraceRunId = traceRunId(message) || selectedTraceRunId"><span class="admin-chat-bubble-avatar" :aria-label="message.sender_type === 'user' ? '管理员' : 'AI 管家'"><template v-if="message.sender_type === 'user'">{{ initials }}</template><img v-else src="/ai-avatar.svg" alt="" /></span><div><ChatMessageContent :message="message" audience="admin" /><time>{{ dateTime(message.sent_at) }}</time></div></article><article v-if="streamingReply"><span class="admin-chat-bubble-avatar"><img src="/ai-avatar.svg" alt="" /></span><div class="agent-stream" aria-live="polite"><p v-if="streamingReply.text">{{ streamingReply.text }}</p><p v-else class="agent-thinking-indicator">正在思考<span>·</span><span>·</span><span>·</span></p><time>正在生成回复…</time></div></article></div>
            <div class="message-quick-actions" aria-label="AI 管家快捷指令"><button v-for="item in aiPrompts" :key="item" type="button" :disabled="busy" @click="useQuickText(item, true)">{{ item }}</button></div><form class="admin-chat-composer" @submit.prevent="send"><textarea v-model="reply" maxlength="4000" placeholder="询问平台概况、用户、店铺、订单或 Agent 运行状态…" @keydown.enter.exact.prevent="send" /><div><span>默认只读 · 右侧同步展示可核验分析</span><button :disabled="!reply.trim() || busy">{{ busy ? '发送中…' : '发送' }}</button></div></form>
          </section>
        </template>

        <template v-else>
          <header class="admin-chat-header"><div><strong>{{ selectedConversation?.participant_type === 'merchant' ? '商家平台服务' : '用户专属客服' }} · {{ selectedConversation?.participant_name }}</strong><small>{{ selectedConversation?.requires_human ? `平台人工接待 · ${selectedTicket?.ticket_status || '同步中'}` : selectedConversation?.participant_type === 'merchant' ? 'AI 经营助理正在接待 · 完整历史已同步' : '专属客服 AI 正在接待 · 完整历史已同步' }}</small></div><div class="actions"><button v-if="canChat" class="secondary small" :disabled="busy" @click="finishHumanService">结束人工服务</button><button class="secondary small" type="button" :disabled="busy" @click="clearHistory">清除记录</button><RouterLink v-if="selectedTicket" :to="`/admin/support/tickets/${selectedTicket.ticket_id}`">完整工作台 ↗</RouterLink></div></header>
          <p v-if="error" class="alert error">{{ error }}</p>
          <div v-if="loading" class="admin-chat-loading">正在读取会话…</div>
          <section v-else-if="selectedConversation" class="admin-chat-conversation">
            <div v-if="selectedConversation.requires_human && !assignedToMe" class="admin-chat-claim compact"><span>◍</span><div><h2>{{ selectedTicket?.ticket_status === 'queued' ? '对方正在等待人工接待' : '会话由其他客服处理' }}</h2><p>历史消息可查看；领取后才可人工回复，避免多人同时处理。</p></div><button v-if="selectedTicket?.ticket_status === 'queued' && auth.has('support:claim')" :disabled="busy" @click="claim">领取会话</button></div>
            <div ref="timeline" class="admin-chat-timeline"><button v-if="supportPreviousCursor" type="button" class="message-history-button" :disabled="loadingEarlier" @click="loadEarlier">{{ loadingEarlier ? '正在读取更早消息…' : '加载更早消息' }}</button><article v-for="message in messages" :key="message.message_id" :class="{ mine: ['human','agent'].includes(message.sender_type), system: message.sender_type === 'system' }"><span v-if="message.sender_type !== 'system'" class="admin-chat-bubble-avatar" :aria-label="message.sender_type === 'human' ? '平台客服' : message.sender_type === 'agent' ? selectedConversation.participant_type === 'merchant' ? 'AI 经营助理' : '专属客服 AI' : selectedConversation.participant_type === 'merchant' ? '店铺' : '用户'"><img v-if="message.sender_type === 'agent'" src="/ai-avatar.svg" alt="" /><img v-else-if="message.sender_type === 'user' && selectedConversation.participant_avatar_url" :src="resolveApiAssetUrl(selectedConversation.participant_avatar_url) || undefined" alt="" /><template v-else>{{ message.sender_type === 'human' ? initials : selectedConversation.participant_name.slice(0, 1) }}</template></span><div><ChatMessageContent :message="message" audience="admin" /><time v-if="message.sender_type !== 'system'">{{ dateTime(message.sent_at) }}</time></div></article><article v-if="streamingReply" class="mine"><span class="admin-chat-bubble-avatar"><img src="/ai-avatar.svg" alt="" /></span><div class="agent-stream" aria-live="polite"><p v-if="streamingReply.text">{{ streamingReply.text }}</p><p v-else class="agent-thinking-indicator">正在思考<span>·</span><span>·</span><span>·</span></p><time>正在生成回复…</time></div></article><p v-if="!messages.length && !streamingReply" class="empty-state">暂无聊天消息</p></div>
            <div class="message-quick-actions" aria-label="人工服务快捷回复"><button v-for="item in selectedConversation.participant_type === 'merchant' ? merchantReplyTemplates : userReplyTemplates" :key="item" type="button" :disabled="busy || !canChat" @click="useQuickText(item, false)">{{ item }}</button></div><form class="admin-chat-composer operator-composer" @submit.prevent="send"><button v-if="selectedConversation.participant_type === 'user'" class="message-plus-button" type="button" :disabled="!canChat" aria-label="发送商品或订单" @click="openAttachments">＋</button><textarea v-model="reply" maxlength="4000" :disabled="!canChat" :placeholder="canChat ? '输入人工回复…' : selectedConversation.requires_human ? '领取后可回复' : 'AI 正在接待；需要转人工时会进入人工队列'" /><div><span>{{ canChat ? '回复会立即同步给对方，并写入 AI 上下文' : 'AI 与人工消息共用同一条会话历史' }}</span><button :disabled="!canChat || !reply.trim() || busy">发送</button></div></form>
          </section>
        </template>
      </main>
      <AgentTracePanel v-if="selected === 'ai'" :messages="aiMessages" :selected-run-id="selectedTraceRunId" :running="traceRunning" :live-trace="liveTrace" title="思考过程" />
      <aside v-else class="operator-context-panel" aria-label="人工服务工作台">
        <header><div><small>当前服务对象</small><strong>{{ selectedConversation?.participant_name || '尚未选择会话' }}</strong></div><span :class="selectedConversation?.requires_human ? 'active' : ''">{{ selectedConversation?.requires_human ? '人工处理中' : 'AI 接待中' }}</span></header>
        <template v-if="selectedConversation">
          <section class="operator-profile-card"><span class="admin-chat-avatar" :class="{ store: selectedConversation.participant_type === 'merchant' }"><img v-if="selectedConversation.participant_avatar_url" :src="resolveApiAssetUrl(selectedConversation.participant_avatar_url) || undefined" alt="" /><template v-else>{{ selectedConversation.participant_name.slice(0, 1) }}</template></span><div><strong>{{ selectedConversation.participant_type === 'merchant' ? '店铺运营人员' : '商城用户' }}</strong><small>{{ selectedConversation.participant_type === 'merchant' ? '平台商家服务' : '平台用户服务' }}</small></div><RouterLink :to="selectedConversation.participant_type === 'merchant' ? '/admin/stores' : `/admin/users/${encodeURIComponent(selectedConversation.participant_id)}`">查看资料 ›</RouterLink></section>
          <section class="operator-ticket-card"><h3>本次服务</h3><dl><div><dt>接待状态</dt><dd>{{ selectedTicket ? selectedTicket.ticket_status : 'AI 接待中' }}</dd></div><div><dt>优先级</dt><dd>{{ selectedTicket?.priority || '普通' }}</dd></div><div><dt>问题摘要</dt><dd>{{ selectedTicket?.handoff_summary || selectedConversation.last_message_preview || '等待对方说明' }}</dd></div></dl></section>
          <section class="operator-context-card"><h3>相关业务</h3><template v-if="workspace?.business_contexts.length"><component :is="businessContextRoute(item.context_type, item.resource_id) ? RouterLink : 'span'" v-for="item in workspace.business_contexts" :key="item.context_id" :to="businessContextRoute(item.context_type, item.resource_id) || undefined"><b>{{ businessContextLabel(item.context_type) }}</b><small>{{ item.resource_id }}</small><i>›</i></component></template><p v-else>当前没有绑定商品、订单或售后对象，可让对方发送对应卡片。</p></section>
          <nav class="operator-action-links"><RouterLink v-if="selectedConversation.participant_type === 'user'" :to="`/admin/users/${encodeURIComponent(selectedConversation.participant_id)}`">用户资料</RouterLink><RouterLink v-if="selectedConversation.participant_type === 'user'" :to="{ path: '/admin/orders', query: { q: selectedConversation.participant_id } }">用户订单</RouterLink><RouterLink v-if="selectedConversation.participant_type === 'merchant'" to="/admin/stores">店铺治理</RouterLink><RouterLink v-if="selectedConversation.participant_type === 'merchant'" to="/admin/orders">店铺订单</RouterLink></nav>
        </template>
        <div v-else class="operator-context-empty">从左侧选择用户或店铺会话</div>
      </aside>
    </section>
    <MessageAttachmentPicker :open="attachmentOpen" :loading="attachmentLoading" :products="attachmentProducts" :orders="attachmentOrders" :sending-id="attachmentSendingId" title="发送给当前用户" product-title="可发送商品" order-title="该用户的订单" @close="attachmentOpen = false" @product="sendPickedProduct" @order="sendPickedOrder" />
  </div>
</template>
