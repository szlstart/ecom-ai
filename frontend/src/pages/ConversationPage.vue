<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { ApiProblem, errorMessage } from '@/api/http'
import { RealtimeConnection, type RealtimeEvent } from '@/api/realtime'
import {
  createClientMessageId,
  cancelHumanServiceTicket,
  getConversation,
  getHumanServiceTicket,
  listMessages,
  putReadCursor,
  requestHumanService,
  sendText,
  type ChatMessage,
  type Conversation,
  type HumanServiceTicket,
} from '@/api/messaging'
import PageState from '@/components/PageState.vue'
import { useUserAuthStore } from '@/stores/user-auth'

type PendingMessage = { clientMessageId: string; text: string; status: 'sending' | 'failed' | 'blocked' }

const route = useRoute()
const router = useRouter()
const auth = useUserAuthStore()
const conversation = ref<Conversation | null>(null)
const messages = ref<ChatMessage[]>([])
const pending = ref<PendingMessage[]>([])
const draft = ref('')
const loading = ref(true)
const sending = ref(false)
const error = ref('')
const connectionState = ref<'connected' | 'polling' | 'offline'>('polling')
const humanOpen = ref(false)
const humanSummary = ref('')
const humanBusy = ref(false)
const humanNotice = ref('')
const humanTicket = ref<HumanServiceTicket | null>(null)
const messageList = ref<HTMLElement | null>(null)
const newBelowCount = ref(0)
const streamingReply = ref<{ runId: string; text: string; chunkIndex: number } | null>(null)
const conversationId = computed(() => String(route.params.conversationId))
let pollTimer: number | undefined
let realtime: RealtimeConnection | undefined
let observer: IntersectionObserver | undefined
let readRetryTimer: number | undefined
let readTarget = 0
let lastReadSubmitted = 0
let readSending = false
let readAttempts = 0
let unreadCursorVersion = 0
const visibleSequences = new Set<number>()
const readTimers = new Map<number, number>()
const elements = new Map<number, HTMLElement>()

function token(): string {
  if (!auth.accessToken) throw new Error('missing user token')
  return auth.accessToken
}
function senderLabel(value: ChatMessage['sender_type']): string {
  return ({ user: '我', agent: '智能客服', human: '人工客服', system: '系统', tool: '服务结果' } as Record<string, string>)[value] ?? value
}
function timeLabel(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(new Date(value))
}
function draftKey(): string { return `ecom-ai:draft:${conversationId.value}` }
function restoreDraft() {
  try {
    const stored = JSON.parse(sessionStorage.getItem(draftKey()) ?? 'null') as { value?: string; expiresAt?: number } | null
    draft.value = stored?.expiresAt && stored.expiresAt > Date.now() ? stored.value ?? '' : ''
    if (!draft.value) sessionStorage.removeItem(draftKey())
  } catch { sessionStorage.removeItem(draftKey()) }
}
function persistDraft(value: string) {
  if (value) sessionStorage.setItem(draftKey(), JSON.stringify({ value, expiresAt: Date.now() + 24 * 60 * 60 * 1000 }))
  else sessionStorage.removeItem(draftKey())
}
function nearBottom(): boolean {
  const element = messageList.value
  return !element || element.scrollHeight - element.scrollTop - element.clientHeight < 96
}
function scrollToBottom() {
  const element = messageList.value
  if (element) element.scrollTop = element.scrollHeight
  newBelowCount.value = 0
}
function mergeMessages(incoming: ChatMessage[], shouldScroll: boolean) {
  const known = new Set(messages.value.map((item) => item.message_id))
  const additions = incoming.filter((item) => !known.has(item.message_id))
  if (!additions.length) return
  messages.value = [...messages.value, ...additions].sort((left, right) => left.sequence_no - right.sequence_no)
  if (!shouldScroll) newBelowCount.value += additions.filter((item) => item.sender_type !== 'user').length
  void nextTick(() => { observeRenderedMessages(); if (shouldScroll) scrollToBottom() })
}
async function refreshHumanTicket() {
  try { humanTicket.value = (await getHumanServiceTicket(conversationId.value, token())).data }
  catch (cause) {
    if (!(cause instanceof ApiProblem) || cause.body.status !== 404) throw cause
    humanTicket.value = null
  }
}
async function load() {
  loading.value = true
  error.value = ''
  clearReadState()
  try {
    const [detail, history] = await Promise.all([
      getConversation(conversationId.value, token()),
      listMessages(conversationId.value, token(), { limit: 100 }),
    ])
    conversation.value = detail.data
    messages.value = history.data.items
    await refreshHumanTicket()
    restoreDraft()
    await nextTick()
    scrollToBottom()
    setupObserver()
    connectionState.value = navigator.onLine ? 'polling' : 'offline'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally { loading.value = false }
}
async function poll() {
  if (loading.value || !navigator.onLine) { connectionState.value = 'offline'; return }
  const afterSequence = messages.value.at(-1)?.sequence_no ?? 0
  const shouldScroll = nearBottom()
  try {
    const response = await listMessages(conversationId.value, token(), { afterSequence, limit: 100 })
    mergeMessages(response.data.items, shouldScroll)
    await refreshHumanTicket()
    connectionState.value = 'polling'
  } catch { connectionState.value = 'offline' }
}
async function handleRealtime(event: RealtimeEvent) {
  if (event.data.conversation_id !== conversationId.value) return
  if (event.type === 'agent.response.started') {
    const runId = event.data.run_id
    if (typeof runId === 'string') streamingReply.value = { runId, text: '', chunkIndex: 0 }
    return
  }
  if (event.type === 'agent.response.delta') {
    const runId = event.data.run_id
    const text = event.data.text_so_far
    const chunkIndex = Number(event.data.chunk_index)
    if (typeof runId !== 'string' || typeof text !== 'string' || !Number.isInteger(chunkIndex)) return
    if (!streamingReply.value || streamingReply.value.runId !== runId) streamingReply.value = { runId, text: '', chunkIndex: 0 }
    if (chunkIndex > streamingReply.value.chunkIndex) streamingReply.value = { runId, text, chunkIndex }
    void nextTick(() => { if (nearBottom()) scrollToBottom() })
    return
  }
  if (event.type === 'agent.response.completed') {
    if (streamingReply.value?.runId === event.data.run_id) streamingReply.value = null
    return
  }
  if (event.type === 'message.created') {
    const message = event.data.message as ChatMessage | undefined
    if (!message || typeof message.message_id !== 'string' || typeof message.sequence_no !== 'number') return
    const lastSequence = messages.value.at(-1)?.sequence_no ?? 0
    const shouldScroll = nearBottom()
    if (message.sequence_no > lastSequence + 1) await poll()
    mergeMessages([message], shouldScroll)
    const runId = message.content?.run_id
    if (typeof runId === 'string' && streamingReply.value?.runId === runId) streamingReply.value = null
    return
  }
  if (event.type === 'support.status.updated' && conversation.value) {
    const status = event.data.ticket_status
    if (typeof status === 'string') {
      if (!humanTicket.value) {
        try { humanTicket.value = (await getHumanServiceTicket(conversationId.value, token())).data }
        catch { /* the REST recovery path will retry on the next poll/reconnect */ }
      } else {
        humanTicket.value.ticket_status = status as HumanServiceTicket['ticket_status']
        humanTicket.value.can_cancel = status === 'queued'
        if (status !== 'queued') humanTicket.value.queue_position = null
      }
    }
    if (status === 'queued') conversation.value.conversation_status = 'human_pending'
    else if (status === 'assigned' || status === 'active' || status === 'waiting_user') conversation.value.conversation_status = 'human_active'
    else if (status === 'resolved' || status === 'closed') conversation.value.conversation_status = 'active'
    humanNotice.value = status === 'active' ? '人工客服已接入。' : status === 'waiting_user' ? '人工客服正在等待你的补充信息。' : status === 'resolved' ? '人工服务已结束，继续发送消息将由智能客服处理。' : humanNotice.value
    return
  }
  if (event.type === 'unread.updated' && conversation.value) {
    const version = Number(event.data.cursor_version ?? 0)
    if (version && version < unreadCursorVersion) return
    if (version) unreadCursorVersion = version
    const unread = Number(event.data.conversation_unread)
    if (Number.isFinite(unread) && unread >= 0) conversation.value.unread_count = unread
  }
}
async function deliver(item: PendingMessage) {
  item.status = 'sending'
  error.value = ''
  try {
    const response = await sendText(conversationId.value, item.text, token(), item.clientMessageId)
    pending.value = pending.value.filter((candidate) => candidate.clientMessageId !== item.clientMessageId)
    if (response.data.message_status === 'hidden') {
      pending.value.push({ ...item, status: 'blocked' })
      return
    }
    mergeMessages([response.data], true)
  } catch (cause) {
    item.status = 'failed'
    error.value = errorMessage(cause)
  }
}
async function send() {
  const text = draft.value.trim()
  if (!text || sending.value) return
  const item: PendingMessage = { clientMessageId: createClientMessageId(), text, status: 'sending' }
  pending.value.push(item)
  draft.value = ''
  persistDraft('')
  sending.value = true
  await nextTick(scrollToBottom)
  try { await deliver(item) }
  finally { sending.value = false }
}
async function retry(item: PendingMessage) { await deliver(item) }
async function requestHuman() {
  const summary = humanSummary.value.trim()
  if (!summary || humanBusy.value) return
  humanBusy.value = true
  error.value = ''
  try {
    const refs = messages.value.slice(-5).map((item) => item.message_id)
    const result = await requestHumanService(conversationId.value, summary, refs, token())
    humanTicket.value = result.data
    humanNotice.value = result.data.ticket_status === 'queued' ? '已进入人工客服队列，请留意会话消息。' : '人工客服已接入。'
    humanOpen.value = false
    humanSummary.value = ''
    if (conversation.value) conversation.value.conversation_status = 'human_pending'
  } catch (cause) { error.value = errorMessage(cause) }
  finally { humanBusy.value = false }
}
async function cancelHuman() {
  if (!humanTicket.value?.can_cancel || humanBusy.value || !window.confirm('确认取消仍在排队的人工服务请求吗？')) return
  humanBusy.value = true
  error.value = ''
  try {
    humanTicket.value = (await cancelHumanServiceTicket(humanTicket.value.ticket_id, token())).data
    humanNotice.value = '已取消人工服务请求。'
    if (conversation.value) conversation.value.conversation_status = 'active'
  } catch (cause) { error.value = errorMessage(cause) }
  finally { humanBusy.value = false }
}
function setMessageElement(element: Element | null, message: ChatMessage) {
  const previous = elements.get(message.sequence_no)
  if (previous && previous !== element) observer?.unobserve(previous)
  if (element instanceof HTMLElement) {
    element.dataset.sequence = String(message.sequence_no)
    elements.set(message.sequence_no, element)
    observer?.observe(element)
  } else { elements.delete(message.sequence_no) }
}
function setupObserver() {
  observer?.disconnect()
  observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const sequence = Number((entry.target as HTMLElement).dataset.sequence)
      if (entry.isIntersecting && entry.intersectionRatio >= 0.5) {
        visibleSequences.add(sequence)
        scheduleRead(sequence)
      } else {
        visibleSequences.delete(sequence)
        const timer = readTimers.get(sequence)
        if (timer) window.clearTimeout(timer)
        readTimers.delete(sequence)
      }
    }
  }, { root: messageList.value, threshold: [0.5] })
  observeRenderedMessages()
}
function observeRenderedMessages() { for (const element of elements.values()) observer?.observe(element) }
function scheduleRead(sequence: number) {
  if (document.visibilityState !== 'visible' || readTimers.has(sequence)) return
  const message = messages.value.find((item) => item.sequence_no === sequence)
  if (!message || message.sender_type === 'user') return
  readTimers.set(sequence, window.setTimeout(() => {
    readTimers.delete(sequence)
    if (!visibleSequences.has(sequence) || document.visibilityState !== 'visible') return
    const highest = Math.max(...[...visibleSequences].filter((candidate) => messages.value.some((item) => item.sequence_no === candidate && item.sender_type !== 'user')))
    if (Number.isFinite(highest) && highest > readTarget) { readTarget = highest; void flushRead() }
  }, 500))
}
async function flushRead() {
  if (readSending || readTarget <= lastReadSubmitted) return
  const target = messages.value.find((item) => item.sequence_no === readTarget)
  if (!target) return
  readSending = true
  try {
    const response = await putReadCursor(conversationId.value, target, token())
    lastReadSubmitted = response.data.last_read_sequence_no
    readAttempts = 0
    if (conversation.value) conversation.value.unread_count = response.data.unread_count
  } catch {
    readAttempts += 1
    if (readAttempts <= 3) readRetryTimer = window.setTimeout(() => void flushRead(), 500 * readAttempts)
  } finally {
    readSending = false
    if (readTarget > lastReadSubmitted && readAttempts === 0) void flushRead()
  }
}
function visibilityChanged() {
  if (document.visibilityState !== 'visible') {
    for (const timer of readTimers.values()) window.clearTimeout(timer)
    readTimers.clear()
    return
  }
  for (const sequence of visibleSequences) scheduleRead(sequence)
  void poll()
}
function clearReadState() {
  observer?.disconnect()
  for (const timer of readTimers.values()) window.clearTimeout(timer)
  readTimers.clear(); visibleSequences.clear(); elements.clear()
  if (readRetryTimer) window.clearTimeout(readRetryTimer)
  readTarget = 0; lastReadSubmitted = 0; readSending = false; readAttempts = 0
}
function onlineChanged() {
  connectionState.value = navigator.onLine ? 'polling' : 'offline'
  if (navigator.onLine) { void poll(); realtime?.start() }
}

watch(draft, persistDraft)
watch(conversationId, async () => { pending.value = []; streamingReply.value = null; newBelowCount.value = 0; await load() })
onMounted(async () => {
  await load()
  realtime = new RealtimeConnection({
    audience: 'user',
    token,
    onEvent: handleRealtime,
    onState: (state) => { connectionState.value = state },
    beforeReconnect: poll,
  })
  realtime.start()
  pollTimer = window.setInterval(() => void poll(), 10_000)
  document.addEventListener('visibilitychange', visibilityChanged)
  window.addEventListener('online', onlineChanged)
  window.addEventListener('offline', onlineChanged)
})
onBeforeUnmount(() => {
  if (pollTimer) window.clearInterval(pollTimer)
  realtime?.stop()
  clearReadState()
  document.removeEventListener('visibilitychange', visibilityChanged)
  window.removeEventListener('online', onlineChanged)
  window.removeEventListener('offline', onlineChanged)
})
</script>

<template>
  <section class="conversation-page">
    <header class="conversation-header">
      <div><RouterLink to="/messages">← 返回会话</RouterLink><h1>{{ conversation?.title || '会话' }}</h1><p class="muted"><span class="connection-dot" :class="connectionState" />{{ connectionState === 'offline' ? '连接中断，恢复后自动补拉' : '已连接，消息按服务端顺序同步' }}</p></div>
      <div class="actions"><RouterLink v-if="conversation?.store_id" :to="`/stores/${conversation.store_id}`">查看店铺</RouterLink><button v-if="!humanTicket || ['resolved','closed'].includes(humanTicket.ticket_status)" type="button" class="secondary" @click="humanOpen = !humanOpen">转人工客服</button></div>
    </header>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <p v-if="humanNotice" class="alert success" role="status">{{ humanNotice }}</p>
    <section v-if="humanTicket && !['resolved','closed'].includes(humanTicket.ticket_status)" class="alert info human-ticket-status" aria-live="polite">
      <span v-if="humanTicket.ticket_status === 'queued'">人工客服排队中<span v-if="humanTicket.queue_position">，当前第 {{ humanTicket.queue_position }} 位</span>。</span>
      <span v-else-if="humanTicket.ticket_status === 'waiting_user'">人工客服正在等待你的补充信息。</span>
      <span v-else>人工客服已接入。</span>
      <button v-if="humanTicket.can_cancel" type="button" class="small secondary" :disabled="humanBusy" @click="cancelHuman">取消排队</button>
    </section>
    <form v-if="humanOpen" class="human-request card" @submit.prevent="requestHuman">
      <label>请简要说明需要人工处理的问题<textarea v-model.trim="humanSummary" minlength="2" maxlength="1000" required /></label>
      <div class="actions"><button type="button" class="secondary" @click="humanOpen = false">取消</button><button :disabled="humanBusy">{{ humanBusy ? '提交中…' : '进入人工队列' }}</button></div>
    </form>
    <PageState :loading="loading" :error="''" :empty="false" @retry="load">
      <div ref="messageList" class="message-timeline" aria-label="聊天消息">
        <p v-if="messages.length === 0 && pending.length === 0" class="conversation-welcome">{{ conversation?.conversation_type === 'exclusive' ? '您好，我是专属客服。您可以咨询平台规则、订单、物流和售后问题。' : '您好，请描述您想咨询的商品或订单问题。' }}</p>
        <article v-for="message in messages" :key="message.message_id" :ref="(element) => setMessageElement(element as Element | null, message)" :class="['message-bubble', message.sender_type === 'user' ? 'mine' : 'theirs']">
          <strong>{{ senderLabel(message.sender_type) }}</strong>
          <p v-if="message.text">{{ message.text }}</p>
          <RouterLink v-else-if="message.message_type === 'product_card' && message.content" class="message-card" :to="`/products/${message.content.product_id}`"><span>商品卡片</span><strong>{{ message.content.product_name }}</strong><small>{{ message.content.sku_name || '查看商品详情' }}</small></RouterLink>
          <RouterLink v-else-if="message.message_type === 'order_card' && message.content" class="message-card" :to="`/me/orders/${message.content.order_id}`"><span>订单卡片</span><strong>{{ message.content.order_id }}</strong><small>状态：{{ message.content.order_status }}</small></RouterLink>
          <small><time :datetime="message.sent_at">{{ timeLabel(message.sent_at) }}</time></small>
        </article>
        <article v-for="item in pending" :key="item.clientMessageId" class="message-bubble mine pending-message">
          <strong>我</strong><p>{{ item.text }}</p><small v-if="item.status === 'sending'">正在发送…</small><small v-else-if="item.status === 'blocked'" class="error-text">内容未通过安全检查，请修改后重新发送。</small><button v-else type="button" class="small danger" @click="retry(item)">发送失败，重试</button>
        </article>
        <article v-if="streamingReply" class="message-bubble theirs agent-stream" aria-live="polite">
          <strong>智能客服</strong><p>{{ streamingReply.text || '正在思考…' }}</p><small>正在生成回复…</small>
        </article>
      </div>
      <button v-if="newBelowCount" type="button" class="new-message-button" @click="scrollToBottom">有 {{ newBelowCount }} 条新消息</button>
    </PageState>
    <form class="message-composer" @submit.prevent="send">
      <label><span class="sr-only">输入消息</span><textarea v-model="draft" maxlength="4000" placeholder="输入消息，Enter 换行" required /></label>
      <button :disabled="sending || !draft.trim()">{{ sending ? '发送中…' : '发送' }}</button>
    </form>
  </section>
</template>
