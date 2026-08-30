<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { ApiProblem, errorMessage } from '@/api/http'
import {
  activateAiMemory,
  decideAgentToolApproval,
  deleteAiMemory,
  getAgentToolApproval,
  grantAfterSaleAgentConsent,
  listAgentConsents,
  listAiMemories,
  revokeAgentConsent,
  type AgentConsent,
  type AgentToolApproval,
  type AiMemoryItem,
} from '@/api/agent-runtime'
import { RealtimeConnection, type RealtimeEvent } from '@/api/realtime'
import {
  createClientMessageId,
  cancelHumanServiceTicket,
  getConversation,
  getHumanServiceTicket,
  listMessages,
  putReadCursor,
  sendOrderCard,
  sendProductCard,
  sendText,
  type ChatMessage,
  type Conversation,
  type HumanServiceTicket,
} from '@/api/messaging'
import PageState from '@/components/PageState.vue'
import ChatMessageContent from '@/components/messaging/ChatMessageContent.vue'
import { confirmAction } from '@/composables/confirmation'
import { useMessageCenterStore } from '@/stores/message-center'
import { useUserAuthStore } from '@/stores/user-auth'

type PendingMessage = { clientMessageId: string; text: string; status: 'sending' | 'failed' | 'blocked' }

const props = withDefaults(defineProps<{ conversationId?: string; embedded?: boolean }>(), {
  conversationId: undefined,
  embedded: false,
})
const emit = defineEmits<{
  'trace-update': [messages: ChatMessage[]]
  'trace-select': [runId: string | null]
}>()

const route = useRoute()
const auth = useUserAuthStore()
const messageCenter = useMessageCenterStore()
const conversation = ref<Conversation | null>(null)
const messages = ref<ChatMessage[]>([])
const previousCursor = ref<string | null>(null)
const loadingEarlier = ref(false)
const pending = ref<PendingMessage[]>([])
const draft = ref('')
const loading = ref(true)
const sending = ref(false)
const sendingCard = ref(false)
const error = ref('')
const connectionState = ref<'connected' | 'polling' | 'offline'>('polling')
const humanBusy = ref(false)
const humanNotice = ref('')
const humanTicket = ref<HumanServiceTicket | null>(null)
const agentConsents = ref<AgentConsent[]>([])
const consentBusy = ref(false)
const consentNotice = ref('')
const approvalStates = ref<Record<string, AgentToolApproval>>({})
const approvalBusy = ref<string | null>(null)
const messageList = ref<HTMLElement | null>(null)
const newBelowCount = ref(0)
const streamingReply = ref<{ runId: string; text: string; chunkIndex: number } | null>(null)
const selectedTraceRunId = ref<string | null>(null)
const memoryStates = ref<Record<string, Pick<AiMemoryItem, 'status' | 'version'>>>({})
const memoryBusy = ref<string | null>(null)
watch(messages, (value) => emit('trace-update', value), { immediate: true })
const conversationId = computed(() => props.conversationId || String(route.params.conversationId))
const activeContext = computed(() => conversation.value?.active_contexts.find((item) => item.status === 'active') ?? null)
const activeAfterSaleConsent = computed(() => agentConsents.value.find((item) => (
  item.consent_type === 'after_sale_write'
  && item.scope_type === 'user'
  && item.status === 'active'
  && (!item.expires_at || apiDate(item.expires_at).getTime() > Date.now())
)) ?? null)
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

function closeEmbeddedNavigation() {
  if (props.embedded) messageCenter.close()
}

function token(): string {
  if (!auth.accessToken) throw new Error('missing user token')
  return auth.accessToken
}
function contextLabel(context: NonNullable<typeof activeContext.value>): string {
  const snapshot = context.display_snapshot
  if (context.context_type === 'order') return `正在咨询订单 ${String(snapshot.order_id ?? context.resource_id)}`
  if (context.context_type === 'product') return `正在咨询商品 ${String(snapshot.name ?? context.resource_id)}`
  if (context.context_type === 'refund') return `正在咨询售后 ${String(snapshot.refund_id ?? context.resource_id)}`
  if (context.context_type === 'shipment') return `正在咨询物流 ${String(snapshot.shipment_id ?? context.resource_id)}`
  if (context.context_type === 'store') return `正在咨询店铺 ${String(snapshot.store_name ?? context.resource_id)}`
  return '正在咨询本次结算中的店铺商品'
}
function timeLabel(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(apiDate(value))
}
function dateTimeLabel(value: string): string {
  const parsed = apiDate(value)
  return Number.isNaN(parsed.getTime())
    ? '—'
    : new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(parsed)
}
function apiDate(value: string): Date {
  return new Date(/(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}Z`)
}
function contentString(message: ChatMessage, key: string): string {
  const value = message.content?.[key]
  return typeof value === 'string' ? value : ''
}
function traceRunId(message: ChatMessage): string | null {
  const trace = message.content?.execution_trace
  if (!trace || typeof trace !== 'object' || Array.isArray(trace)) return null
  const runId = (trace as Record<string, unknown>).run_id
  return typeof runId === 'string' ? runId : null
}
function selectTrace(message: ChatMessage) {
  const runId = traceRunId(message)
  if (!runId) return
  selectedTraceRunId.value = runId
  emit('trace-select', runId)
}
function contentNumber(message: ChatMessage, key: string): number | null {
  const value = message.content?.[key]
  return typeof value === 'number' && Number.isSafeInteger(value) ? value : null
}
function evidenceLabel(message: ChatMessage): string {
  const value = message.content?.evidence_file_ids
  return Array.isArray(value) && value.length ? `${value.length} 个凭证文件` : '未提供'
}
function approvalId(message: ChatMessage): string { return contentString(message, 'approval_id') }
function memoryId(message: ChatMessage): string { return contentString(message, 'memory_id') }
function memoryState(message: ChatMessage): Pick<AiMemoryItem, 'status' | 'version'> {
  const id = memoryId(message)
  return memoryStates.value[id] ?? {
    status: contentString(message, 'memory_status') as AiMemoryItem['status'] || 'candidate',
    version: contentNumber(message, 'memory_version') ?? 0,
  }
}
function approvalFor(message: ChatMessage): AgentToolApproval | null {
  return approvalStates.value[approvalId(message)] ?? null
}
function approvalStatus(message: ChatMessage): AgentToolApproval['approval_status'] {
  return approvalFor(message)?.approval_status ?? 'pending'
}
function approvalStatusLabel(message: ChatMessage): string {
  return ({ pending: '等待确认', approved: '已确认，正在提交', rejected: '已拒绝', expired: '已过期', consumed: '已处理' })[approvalStatus(message)]
}
function requestedAmountLabel(message: ChatMessage): string {
  const value = message.content?.requested_amount
  if (!value || typeof value !== 'object') return '—'
  const amount = Number((value as Record<string, unknown>).amount)
  const currency = String((value as Record<string, unknown>).currency ?? 'CNY')
  if (!Number.isSafeInteger(amount)) return '—'
  return new Intl.NumberFormat('zh-CN', { style: 'currency', currency }).format(amount / 100)
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
  void refreshApprovals(additions)
  if (!shouldScroll) newBelowCount.value += additions.filter((item) => item.sender_type !== 'user').length
  void nextTick(() => { observeRenderedMessages(); if (shouldScroll) scrollToBottom() })
}
async function refreshAgentConsents() {
  agentConsents.value = (await listAgentConsents(token())).data.items
}
async function refreshApprovals(candidates: ChatMessage[] = messages.value) {
  const ids = [...new Set(candidates.filter((item) => item.message_type === 'refund_approval').map(approvalId).filter(Boolean))]
  await Promise.all(ids.map(async (id) => {
    try {
      const state = (await getAgentToolApproval(id, token())).data
      approvalStates.value = { ...approvalStates.value, [id]: state }
    } catch (cause) {
      if (!(cause instanceof ApiProblem) || cause.body.status !== 404) throw cause
    }
  }))
}
async function refreshMemoryCards(candidates: ChatMessage[] = messages.value) {
  if (!candidates.some((item) => item.message_type === 'memory_candidate')) return
  const items = (await listAiMemories(token())).data.items
  memoryStates.value = Object.fromEntries(items.map((item) => [item.memory_id, { status: item.status, version: item.version }]))
}
async function decideMemory(message: ChatMessage, decision: 'activate' | 'reject') {
  const id = memoryId(message)
  const state = memoryState(message)
  if (!id || state.status !== 'candidate' || memoryBusy.value) return
  memoryBusy.value = id
  error.value = ''; consentNotice.value = ''
  try {
    if (decision === 'activate') {
      const activated = (await activateAiMemory(id, state.version, token())).data
      memoryStates.value = { ...memoryStates.value, [id]: { status: activated.status, version: activated.version } }
      consentNotice.value = '偏好已在你的明确确认后写入长期记忆，可随时在 AI 个性化与记忆中更正或删除。'
    } else {
      await deleteAiMemory(id, state.version, token())
      memoryStates.value = { ...memoryStates.value, [id]: { status: 'deleted', version: state.version + 1 } }
      consentNotice.value = '候选偏好已拒绝并停止使用。'
    }
  } catch (cause) { error.value = errorMessage(cause); await refreshMemoryCards([message]) }
  finally { memoryBusy.value = null }
}
async function grantAfterSaleConsent() {
  if (consentBusy.value || !await confirmAction('授权专属客服在未来 30 天内协助准备售后申请草稿？实际提交仍必须由你逐次点击确认。')) return
  consentBusy.value = true
  error.value = ''
  try {
    const expiresAt = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000).toISOString()
    const granted = (await grantAfterSaleAgentConsent(token(), expiresAt)).data
    agentConsents.value = [granted, ...agentConsents.value]
    consentNotice.value = '售后协助授权已生效；退款提交仍需逐次核对并点击确认。'
  } catch (cause) { error.value = errorMessage(cause) }
  finally { consentBusy.value = false }
}
async function revokeAfterSaleConsent() {
  const consent = activeAfterSaleConsent.value
  if (!consent || consentBusy.value || !await confirmAction('确认撤销专属客服的售后协助授权吗？未提交的审批将无法继续执行。', { tone: 'danger' })) return
  consentBusy.value = true
  error.value = ''
  try {
    const revoked = (await revokeAgentConsent(consent.consent_id, token())).data
    agentConsents.value = agentConsents.value.map((item) => item.consent_id === revoked.consent_id ? revoked : item)
    consentNotice.value = '售后协助授权已撤销。'
  } catch (cause) { error.value = errorMessage(cause) }
  finally { consentBusy.value = false }
}
async function decideApproval(message: ChatMessage, decision: 'approve' | 'reject') {
  const id = approvalId(message)
  const state = approvalFor(message)
  const cardVersion = Number(message.content?.approval_version)
  const version = state?.version ?? cardVersion
  if (!id || !Number.isSafeInteger(version) || approvalBusy.value) return
  const prompt = decision === 'approve'
    ? `确认提交订单 ${contentString(message, 'order_id')} 的退款申请，申请金额 ${requestedAmountLabel(message)}？`
    : '确认拒绝并关闭这份退款申请草稿吗？'
  if (!await confirmAction(prompt, { tone: decision === 'reject' ? 'danger' : 'default' })) return
  approvalBusy.value = id
  error.value = ''
  try {
    const result = (await decideAgentToolApproval(id, decision, version, token())).data
    approvalStates.value = { ...approvalStates.value, [id]: result }
    consentNotice.value = decision === 'approve' ? '已确认提交，专属客服正在处理，请勿重复操作。' : '已拒绝该退款申请草稿。'
    window.setTimeout(() => void poll(), 600)
  } catch (cause) {
    error.value = errorMessage(cause)
    await refreshApprovals([message])
  } finally { approvalBusy.value = null }
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
    previousCursor.value = history.data.previous_cursor
    await Promise.all([
      refreshHumanTicket(),
      detail.data.conversation_type === 'exclusive' ? refreshAgentConsents() : Promise.resolve(),
      refreshApprovals(history.data.items),
      refreshMemoryCards(history.data.items),
    ])
    restoreDraft()
    await nextTick()
    scrollToBottom()
    setupObserver()
    connectionState.value = navigator.onLine ? 'polling' : 'offline'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally { loading.value = false }
}
async function loadEarlier() {
  const cursor = previousCursor.value
  const element = messageList.value
  if (!cursor || !element || loadingEarlier.value) return
  loadingEarlier.value = true
  const previousHeight = element.scrollHeight
  const previousTop = element.scrollTop
  try {
    const response = await listMessages(conversationId.value, token(), { cursor, limit: 100 })
    const known = new Set(messages.value.map((item) => item.message_id))
    const additions = response.data.items.filter((item) => !known.has(item.message_id))
    messages.value = [...additions, ...messages.value]
    previousCursor.value = response.data.previous_cursor
    await refreshApprovals(additions)
    await nextTick()
    element.scrollTop = previousTop + element.scrollHeight - previousHeight
    observeRenderedMessages()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loadingEarlier.value = false }
}
function timelineScrolled() {
  if ((messageList.value?.scrollTop ?? 999) < 48) void loadEarlier()
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
async function sendActiveContextCard() {
  const context = activeContext.value
  if (!context || sendingCard.value || !['product', 'order'].includes(context.context_type)) return
  sendingCard.value = true
  error.value = ''
  try {
    const response = context.context_type === 'product'
      ? await sendProductCard(
        conversationId.value,
        context.resource_id,
        typeof context.display_snapshot.sku_id === 'string' ? context.display_snapshot.sku_id : null,
        token(),
      )
      : await sendOrderCard(conversationId.value, context.resource_id, token())
    mergeMessages([response.data], true)
  } catch (cause) { error.value = errorMessage(cause) }
  finally { sendingCard.value = false }
}
async function retry(item: PendingMessage) { await deliver(item) }
async function cancelHuman() {
  if (!humanTicket.value?.can_cancel || humanBusy.value || !await confirmAction('确认取消仍在排队的人工服务请求吗？')) return
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
watch(conversationId, async () => { pending.value = []; streamingReply.value = null; newBelowCount.value = 0; previousCursor.value = null; agentConsents.value = []; approvalStates.value = {}; selectedTraceRunId.value = null; emit('trace-select', null); await load() })
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
  <section class="conversation-page" :class="{ embedded }">
    <header class="conversation-header">
      <div><RouterLink v-if="!embedded" to="/messages">← 返回会话</RouterLink><h1 v-if="!embedded">{{ conversation?.title || '会话' }}</h1><strong v-else>{{ conversation?.title || '正在读取会话' }}</strong><p class="muted"><span class="connection-dot" :class="connectionState" />{{ connectionState === 'offline' ? '连接中断，恢复后自动补拉' : '消息实时同步' }}</p></div>
      <div class="actions"><RouterLink v-if="conversation?.store_id" :to="`/stores/${conversation.store_id}`" @click="closeEmbeddedNavigation">查看店铺</RouterLink></div>
    </header>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <p v-if="humanNotice" class="alert success" role="status">{{ humanNotice }}</p>
    <p v-if="consentNotice" class="alert success" role="status">{{ consentNotice }}</p>
    <p v-if="activeContext" class="alert info conversation-context" role="status">
      <strong>{{ contextLabel(activeContext) }}</strong>
      <span>客服回答与工具调用将绑定此上下文版本 v{{ activeContext.context_version }}。</span>
    </p>
    <details v-if="conversation?.conversation_type === 'exclusive'" class="agent-consent-card" aria-labelledby="after-sale-consent-heading">
      <summary>
        <span class="agent-consent-icon" aria-hidden="true">盾</span>
        <span><strong id="after-sale-consent-heading">售后协助授权</strong><small>{{ activeAfterSaleConsent ? '已授权，可为当前订单准备售后草稿' : '按需授权，不会自动提交退款' }}</small></span>
        <em>管理授权</em>
      </summary>
      <div class="agent-consent-body">
        <p v-if="activeAfterSaleConsent">已授权专属客服准备售后草稿<span v-if="activeAfterSaleConsent.expires_at">，有效期至 {{ dateTimeLabel(activeAfterSaleConsent.expires_at) }}</span>。每次退款提交仍需你核对卡片并点击确认。</p>
        <p v-else>授权后，专属客服可以根据你的当前订单准备售后草稿；授权本身不会创建退款，聊天中的“确认”也不会触发提交。</p>
        <button v-if="activeAfterSaleConsent" type="button" class="small secondary" :disabled="consentBusy" @click="revokeAfterSaleConsent">{{ consentBusy ? '处理中…' : '撤销授权' }}</button>
        <button v-else type="button" class="small" :disabled="consentBusy" @click="grantAfterSaleConsent">{{ consentBusy ? '授权中…' : '授权售后协助 30 天' }}</button>
      </div>
    </details>
    <section v-if="humanTicket && !['resolved','closed'].includes(humanTicket.ticket_status)" class="alert info human-ticket-status" aria-live="polite">
      <span v-if="humanTicket.ticket_status === 'queued'">人工客服排队中<span v-if="humanTicket.queue_position">，当前第 {{ humanTicket.queue_position }} 位</span>。</span>
      <span v-else-if="humanTicket.ticket_status === 'waiting_user'">人工客服正在等待你的补充信息。</span>
      <span v-else>人工客服已接入。</span>
      <button v-if="humanTicket.can_cancel" type="button" class="small secondary" :disabled="humanBusy" @click="cancelHuman">取消排队</button>
    </section>
    <PageState :loading="loading" :error="''" :empty="false" @retry="load">
      <div ref="messageList" class="message-timeline" aria-label="聊天消息" @scroll.passive="timelineScrolled">
        <button v-if="previousCursor" type="button" class="message-history-button" :disabled="loadingEarlier" @click="loadEarlier">{{ loadingEarlier ? '正在读取更早消息…' : '加载更早消息' }}</button>
        <p v-if="messages.length === 0 && pending.length === 0" class="conversation-welcome">{{ conversation?.conversation_type === 'exclusive' ? '您好，我是专属客服。您可以咨询平台规则、订单、物流和售后问题。' : '您好，请描述您想咨询的商品或订单问题。' }}</p>
        <div v-for="message in messages" :key="message.message_id" :class="['message-row', message.sender_type === 'user' ? 'mine' : 'theirs']">
          <span class="message-avatar" :class="{ agent: message.sender_type === 'agent' }" aria-hidden="true">{{ message.sender_type === 'user' ? '👤' : message.sender_type === 'agent' ? '✦' : message.sender_type === 'human' ? '🧑' : '⚙' }}</span>
          <article :ref="(element) => setMessageElement(element as Element | null, message)" :class="['message-bubble', message.sender_type === 'user' ? 'mine' : 'theirs', { 'trace-selectable': traceRunId(message), 'trace-selected': traceRunId(message) === selectedTraceRunId }]" @click="selectTrace(message)">
          <ChatMessageContent :message="message" audience="user" @navigate="closeEmbeddedNavigation" />
          <section v-if="message.message_type === 'refund_approval' && message.content" class="refund-approval-card" :aria-label="`退款申请确认：${approvalStatusLabel(message)}`">
            <header><strong>退款申请确认</strong><span class="badge">{{ approvalStatusLabel(message) }}</span></header>
            <dl>
              <div><dt>订单</dt><dd>{{ contentString(message, 'order_id') }}</dd></div>
              <div><dt>商品</dt><dd>{{ contentString(message, 'product_name') }} · {{ contentString(message, 'sku_name') }}</dd></div>
              <div><dt>数量</dt><dd>{{ contentNumber(message, 'quantity') ?? '—' }} 件</dd></div>
              <div><dt>退款方式</dt><dd>{{ contentString(message, 'refund_type') === 'return_and_refund' ? '退货退款' : '仅退款' }}</dd></div>
              <div><dt>申请金额</dt><dd><strong>{{ requestedAmountLabel(message) }}</strong></dd></div>
              <div><dt>原因</dt><dd>{{ contentString(message, 'reason_detail') }}</dd></div>
              <div><dt>凭证</dt><dd>{{ evidenceLabel(message) }}</dd></div>
              <div><dt>规则版本</dt><dd>{{ contentString(message, 'policy_version') }}</dd></div>
              <div><dt>有效期</dt><dd>{{ dateTimeLabel(contentString(message, 'expires_at')) }}</dd></div>
            </dl>
            <p class="approval-warning">点击确认后才会提交；在聊天框输入“确认”不会产生退款申请。</p>
            <div v-if="approvalStatus(message) === 'pending'" class="actions">
              <button type="button" class="secondary" :disabled="approvalBusy === approvalId(message)" @click="decideApproval(message, 'reject')">拒绝</button>
              <button type="button" :disabled="approvalBusy === approvalId(message)" @click="decideApproval(message, 'approve')">{{ approvalBusy === approvalId(message) ? '处理中…' : '核对无误，确认提交' }}</button>
            </div>
          </section>
          <section v-if="message.message_type === 'memory_candidate' && message.content" class="memory-candidate-card" :aria-label="`长期记忆候选：${memoryState(message).status}`">
            <header><div><span>◉</span><strong>长期记忆候选</strong></div><b>{{ memoryState(message).status === 'candidate' ? '等待你确认' : memoryState(message).status === 'active' ? '已记住' : '未采用' }}</b></header>
            <blockquote>{{ contentString(message, 'memory_value') }}</blockquote>
            <dl><div><dt>类型</dt><dd>{{ contentString(message, 'memory_type') === 'constraint' ? '购物约束' : '购物偏好' }}</dd></div><div><dt>有效期</dt><dd>{{ dateTimeLabel(contentString(message, 'expires_at')) }}</dd></div></dl>
            <p>只有点击“确认记住”才会生效；订单、价格、库存等实时事实不会从长期记忆读取。</p>
            <div v-if="memoryState(message).status === 'candidate'" class="actions"><button type="button" class="secondary" :disabled="memoryBusy === memoryId(message)" @click.stop="decideMemory(message, 'reject')">不记住</button><button type="button" :disabled="memoryBusy === memoryId(message)" @click.stop="decideMemory(message, 'activate')">{{ memoryBusy === memoryId(message) ? '处理中…' : '确认记住' }}</button></div>
          </section>
          <small><time :datetime="message.sent_at">{{ timeLabel(message.sent_at) }}</time></small>
          </article>
        </div>
        <div v-for="item in pending" :key="item.clientMessageId" class="message-row mine"><span class="message-avatar" aria-hidden="true">👤</span><article class="message-bubble mine pending-message">
          <p>{{ item.text }}</p><small v-if="item.status === 'sending'">正在发送…</small><small v-else-if="item.status === 'blocked'" class="error-text">内容未通过安全检查，请修改后重新发送。</small><button v-else type="button" class="small danger" @click="retry(item)">发送失败，重试</button>
        </article></div>
        <div v-if="streamingReply" class="message-row theirs"><span class="message-avatar agent" aria-hidden="true">✦</span><article class="message-bubble theirs agent-stream" aria-live="polite">
          <p>{{ streamingReply.text || '正在思考…' }}</p><small>正在生成回复…</small>
        </article></div>
      </div>
      <button v-if="newBelowCount" type="button" class="new-message-button" @click="scrollToBottom">有 {{ newBelowCount }} 条新消息</button>
    </PageState>
    <form class="message-composer rich-message-composer" @submit.prevent="send">
      <div class="message-composer-toolbar">
        <button v-if="activeContext && ['product','order'].includes(activeContext.context_type)" type="button" class="small secondary" :disabled="sendingCard" @click="sendActiveContextCard">{{ sendingCard ? '发送中…' : activeContext.context_type === 'product' ? '▧ 发送当前商品' : '▤ 发送当前订单' }}</button>
        <span>Enter 发送 · Shift + Enter 换行</span>
      </div>
      <label><span class="sr-only">输入消息</span><textarea v-model="draft" maxlength="4000" placeholder="输入消息…" required @keydown.enter.exact.prevent="send" /></label>
      <button :disabled="sending || !draft.trim()">{{ sending ? '发送中…' : '发送' }}</button>
    </form>
  </section>
</template>
