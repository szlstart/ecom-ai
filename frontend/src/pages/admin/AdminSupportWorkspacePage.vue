<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import {
  claimSupportTicket,
  createSupportNote,
  getSupportWorkspace,
  listSupportMessages,
  listSupportNotes,
  putSupportReadCursor,
  resolveSupportTicket,
  resumeSupportTicket,
  sendSupportMessage,
  transferSupportTicket,
  waitSupportTicket,
  type SupportInternalNote,
  type SupportWorkspace,
} from '@/api/admin-support'
import { errorMessage } from '@/api/http'
import { RealtimeConnection, type RealtimeEvent, type RealtimeState } from '@/api/realtime'
import { createClientMessageId, type ChatMessage } from '@/api/messaging'
import PageState from '@/components/PageState.vue'
import { confirmAction } from '@/composables/confirmation'
import { useAdminAuthStore } from '@/stores/admin-auth'

const props = withDefaults(defineProps<{ portal?: 'admin' | 'merchant' }>(), { portal: 'admin' })

const route = useRoute()
const auth = useAdminAuthStore()
const ticketId = String(route.params.ticketId)
const workspace = ref<SupportWorkspace | null>(null)
const messages = ref<ChatMessage[]>([])
const notes = ref<SupportInternalNote[]>([])
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const reply = ref('')
const failedReply = ref<{ id: string; text: string } | null>(null)
const waitReason = ref('')
const transferUserId = ref('')
const transferReason = ref('')
const resolutionSummary = ref('')
const resolutionInternalNote = ref('')
const noteText = ref('')
const noteType = ref('handling')
const connectionState = ref<RealtimeState>('polling')
let pollTimer: number | undefined
let realtime: RealtimeConnection | undefined
let readTimer: number | undefined
let lastReadSequence = 0
const ticket = computed(() => workspace.value?.ticket ?? null)
const assignedToMe = computed(() => ticket.value?.assigned_user_id === auth.userId)

function token(): string { if (!auth.accessToken) throw new Error('missing admin token'); return auth.accessToken }
function can(code: string): boolean { return auth.has(code) }
function statusLabel(value: string): string { return ({ queued: '待领取', assigned: '待接受', active: '处理中', waiting_user: '等待用户', resolved: '已解决', closed: '已关闭' } as Record<string, string>)[value] ?? value }
function senderLabel(value: string): string { return ({ user: '用户', agent: 'AI 客服', human: '人工客服', system: '系统', tool: '工具结果' } as Record<string, string>)[value] ?? value }
function dateTime(value: string): string { return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(value)) }
async function load() {
  loading.value = true; error.value = ''
  try {
    workspace.value = (await getSupportWorkspace(ticketId, token())).data
    await loadPrivatePanels()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}
async function loadPrivatePanels() {
  if (!ticket.value || !assignedToMe.value || !['assigned', 'active', 'waiting_user'].includes(ticket.value.ticket_status)) { messages.value = []; notes.value = []; return }
  try {
    messages.value = (await listSupportMessages(ticket.value.conversation_id, token())).data.items
    scheduleSupportRead()
  }
  catch (cause) { error.value = errorMessage(cause) }
  if (can('support:internal_notes_read')) {
    try { notes.value = (await listSupportNotes(ticket.value.ticket_id, token())).data.items }
    catch (cause) { error.value = errorMessage(cause) }
  }
}
function scheduleSupportRead() {
  if (readTimer) window.clearTimeout(readTimer)
  const latest = [...messages.value].reverse().find((item) => item.sender_type === 'user')
  if (!latest || latest.sequence_no <= lastReadSequence || document.visibilityState !== 'visible') return
  readTimer = window.setTimeout(async () => {
    readTimer = undefined
    if (!ticket.value || document.visibilityState !== 'visible') return
    try {
      const result = await putSupportReadCursor(ticket.value.conversation_id, latest, token())
      lastReadSequence = result.data.last_read_sequence_no
    } catch { /* polling/reconnect will retry after the message is visible again */ }
  }, 500)
}
async function refreshQuietly() { if (!document.hidden && assignedToMe.value) await loadPrivatePanels() }
async function handleRealtime(event: RealtimeEvent) {
  if (!ticket.value || event.data.conversation_id !== ticket.value.conversation_id) return
  if (event.type === 'message.created') {
    const message = event.data.message as ChatMessage | undefined
    if (!message || typeof message.message_id !== 'string' || typeof message.sequence_no !== 'number') return
    const last = messages.value.at(-1)?.sequence_no ?? 0
    if (message.sequence_no > last + 1) await loadPrivatePanels()
    if (!messages.value.some((item) => item.message_id === message.message_id)) {
      messages.value = [...messages.value, message].sort((left, right) => left.sequence_no - right.sequence_no)
      scheduleSupportRead()
    }
  } else if (event.type === 'support.ticket.updated') {
    await load()
  }
}
async function run(action: () => Promise<unknown>, success: string) {
  if (busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try { await action(); notice.value = success; await load() }
  catch (cause) { error.value = errorMessage(cause); await load() }
  finally { busy.value = false }
}
async function claim() { if (ticket.value) await run(() => claimSupportTicket(ticket.value!, token()), '工单领取成功，AI 面向用户的自动回复保持暂停。') }
async function waitUser() { if (ticket.value && waitReason.value.trim()) await run(() => waitSupportTicket(ticket.value!, 'NEED_MORE_INFO', waitReason.value.trim(), token()), '已进入等待用户状态，SLA 已暂停。') }
async function resume() { if (ticket.value) await run(() => resumeSupportTicket(ticket.value!, token()), '工单已恢复处理。') }
async function transfer() { if (ticket.value && transferUserId.value.trim() && transferReason.value.trim() && await confirmAction('确认将工单转派给该客服吗？')) await run(() => transferSupportTicket(ticket.value!, transferUserId.value.trim(), transferReason.value.trim(), token()), '工单已转派。') }
async function resolve() { if (ticket.value && resolutionSummary.value.trim() && await confirmAction('确认结束人工服务吗？结束后只恢复 AI 可用状态，不会自动生成回复。')) await run(() => resolveSupportTicket(ticket.value!, 'ANSWERED', resolutionSummary.value.trim(), resolutionInternalNote.value.trim() || null, token()), '人工服务已结束。') }
async function saveNote() { if (ticket.value && noteText.value.trim()) await run(async () => { await createSupportNote(ticket.value!, noteText.value.trim(), noteType.value, token()); noteText.value = '' }, '内部备注已保存，仅授权客服可见。') }
async function deliverReply(id: string, text: string) {
  if (!ticket.value) return
  busy.value = true; error.value = ''
  try {
    const response = await sendSupportMessage(ticket.value.conversation_id, text, token(), id)
    failedReply.value = null
    if (response.data.message_status === 'hidden') { error.value = '回复未通过内容安全检查，未向用户发送。'; return }
    messages.value.push(response.data)
  } catch (cause) { failedReply.value = { id, text }; error.value = errorMessage(cause) }
  finally { busy.value = false }
}
async function sendReply() { const text = reply.value.trim(); if (!text || busy.value) return; const id = createClientMessageId(); reply.value = ''; await deliverReply(id, text) }
onMounted(async () => {
  await load()
  realtime = new RealtimeConnection({
    audience: 'admin', token, onEvent: handleRealtime,
    onState: (state) => { connectionState.value = state },
    beforeReconnect: refreshQuietly,
  })
  realtime.start()
  pollTimer = window.setInterval(() => void refreshQuietly(), 10_000)
  document.addEventListener('visibilitychange', scheduleSupportRead)
})
onBeforeUnmount(() => {
  if (pollTimer) window.clearInterval(pollTimer)
  if (readTimer) window.clearTimeout(readTimer)
  realtime?.stop()
  document.removeEventListener('visibilitychange', scheduleSupportRead)
})
</script>

<template>
  <section class="admin-page-stack support-workspace-page">
    <header class="page-heading"><div><p class="eyebrow">{{ props.portal === 'merchant' ? '店铺客服' : '人工客服工作台' }}</p><h1>{{ ticketId }}</h1><p class="muted"><span class="connection-dot" :class="connectionState" />用户可见消息、交接资料和内部备注严格分区。</p></div><RouterLink :to="props.portal === 'merchant' ? '/merchant/support' : '/admin/support/tickets'">返回工单队列</RouterLink></header>
    <p v-if="notice" class="alert success" role="status">{{ notice }}</p><p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <PageState :loading="loading" :error="''" :empty="!workspace" empty-title="工单不存在或不在当前数据范围" @retry="load">
      <template v-if="workspace && ticket">
        <section class="support-workspace-grid">
          <aside class="support-ticket-panel card">
            <div class="card-heading"><div><h2>{{ statusLabel(ticket.ticket_status) }}</h2><p>{{ ticket.queue_code }}</p></div><span class="badge">{{ ticket.priority }}</span></div>
            <dl class="detail-list"><dt>用户</dt><dd>{{ workspace.user.nickname }} · {{ workspace.user.user_id }}</dd><dt>队列</dt><dd>{{ ticket.queue_type }}</dd><dt>当前客服</dt><dd>{{ ticket.assigned_user_id || '未领取' }}</dd><dt>SLA</dt><dd>{{ ticket.sla_due_at ? dateTime(ticket.sla_due_at) : ticket.ticket_status === 'waiting_user' ? '暂停中' : '—' }}</dd><dt>交接策略</dt><dd>{{ ticket.handoff_policy_version }}</dd></dl>
            <div class="handoff-box"><strong>交接摘要</strong><p>{{ ticket.handoff_summary }}</p></div>
            <button v-if="ticket.ticket_status === 'queued' && can('support:claim')" :disabled="busy" @click="claim">领取工单</button>
            <button v-if="ticket.ticket_status === 'assigned' && assignedToMe && can('support:claim')" :disabled="busy" @click="claim">接受转派</button>
            <template v-if="assignedToMe && ['active','waiting_user'].includes(ticket.ticket_status)">
              <form v-if="ticket.ticket_status !== 'waiting_user' && can('support:wait')" class="admin-editor" @submit.prevent="waitUser"><label>等待用户原因<textarea v-model.trim="waitReason" maxlength="1000" required /></label><button class="secondary" :disabled="busy">等待用户</button></form>
              <button v-if="ticket.ticket_status === 'waiting_user' && can('support:resume')" class="secondary" :disabled="busy" @click="resume">恢复处理</button>
              <form v-if="can('support:transfer')" class="admin-editor" @submit.prevent="transfer"><label>目标客服公开 ID<input v-model.trim="transferUserId" required /></label><label>转派原因<textarea v-model.trim="transferReason" maxlength="500" required /></label><button class="secondary" :disabled="busy">确认转派</button></form>
              <form v-if="can('support:resolve')" class="admin-editor" @submit.prevent="resolve"><label>用户可见的解决摘要<textarea v-model.trim="resolutionSummary" maxlength="1000" required /></label><label>结束处理内部说明（用户不可见）<textarea v-model.trim="resolutionInternalNote" maxlength="1000" /></label><button class="danger" :disabled="busy">结束人工服务</button></form>
            </template>
          </aside>
          <main class="support-conversation-panel card">
            <h2>当前会话（用户可见）</h2>
            <div v-if="assignedToMe" class="support-message-list" aria-live="polite"><article v-for="message in messages" :key="message.message_id" :class="['support-message', message.sender_type === 'human' ? 'mine' : '']"><strong>{{ senderLabel(message.sender_type) }}</strong><p>{{ message.text || (message.message_type === 'product_card' ? '[商品卡片]' : message.message_type === 'order_card' ? '[订单卡片]' : '[系统消息]') }}</p><small>{{ dateTime(message.sent_at) }}</small></article><p v-if="!messages.length" class="muted">暂无可见消息。</p></div>
            <div v-else><p class="alert info">领取工单后才能读取完整用户可见会话。领取前仅显示交接所引用的必要消息。</p><article v-for="message in workspace.referenced_messages" :key="message.message_id" class="support-message"><strong>{{ senderLabel(message.sender_type) }}</strong><p>{{ message.text || `[${message.message_type}]` }}</p></article></div>
            <form v-if="assignedToMe && can('support:reply') && ticket.ticket_status === 'active'" class="message-composer" @submit.prevent="sendReply"><label><span class="sr-only">回复用户</span><textarea v-model="reply" maxlength="4000" required placeholder="输入用户可见回复" /></label><button :disabled="busy || !reply.trim()">发送</button></form>
            <p v-if="failedReply" class="alert error">上一条回复发送失败。<button type="button" class="small danger" @click="deliverReply(failedReply.id, failedReply.text)">使用同一消息 ID 重试</button></p>
          </main>
          <aside class="support-assist-panel card">
            <h2>受控业务上下文</h2><p v-if="!workspace.business_contexts.length" class="muted">没有绑定业务上下文。</p><article v-for="context in workspace.business_contexts" :key="context.context_id" class="business-context-card"><strong>{{ context.context_type }}</strong><small>{{ context.resource_id }} · v{{ context.context_version }}</small><pre>{{ JSON.stringify(context.display_snapshot, null, 2) }}</pre></article>
            <template v-if="assignedToMe && can('support:internal_notes_read')"><h2>内部备注</h2><div class="internal-note-list"><article v-for="note in notes" :key="note.note_id"><strong>{{ note.note_type }}</strong><p>{{ note.text }}</p><small>{{ note.author_user_id }} · {{ dateTime(note.created_at) }}</small></article><p v-if="!notes.length" class="muted">暂无内部备注。</p></div></template>
            <form v-if="assignedToMe && can('support:internal_notes_write')" class="admin-editor internal-note-editor" @submit.prevent="saveNote"><h3>新增内部备注</h3><label>类型<select v-model="noteType"><option value="handling">处理</option><option value="transfer">转派</option><option value="risk">风险</option><option value="resolution">结案</option></select></label><label>内容<textarea v-model.trim="noteText" maxlength="4000" required /></label><button class="secondary" :disabled="busy">保存内部备注</button></form>
            <h2>状态事件</h2><ol class="timeline"><li v-for="event in workspace.events" :key="event.event_id"><strong>{{ event.event_type }}</strong><span>{{ event.from_status || '创建' }} → {{ event.to_status }}</span><p>{{ event.reason || event.reason_code || '—' }}</p><time>{{ dateTime(event.occurred_at) }}</time></li></ol>
          </aside>
        </section>
      </template>
    </PageState>
  </section>
</template>
