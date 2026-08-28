<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

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
import { createClientMessageId, type ChatMessage } from '@/api/messaging'
import { useAdminAuthStore } from '@/stores/admin-auth'

const emit = defineEmits<{ close: []; 'unread-change': [count: number] }>()
const auth = useAdminAuthStore()
const tickets = ref<SupportTicket[]>([])
const selected = ref<'ai' | string>('ai')
const workspace = ref<SupportWorkspace | null>(null)
const messages = ref<ChatMessage[]>([])
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const reply = ref('')
const search = ref('')
const userGroupOpen = ref(true)
const storeGroupOpen = ref(true)

const selectedTicket = computed(() => tickets.value.find((item) => item.ticket_id === selected.value) ?? null)
const assignedToMe = computed(() => selectedTicket.value?.assigned_user_id === auth.userId)
const canChat = computed(() => assignedToMe.value && selectedTicket.value?.ticket_status === 'active')
const filteredTickets = computed(() => {
  const keyword = search.value.trim().toLocaleLowerCase()
  return keyword ? tickets.value.filter((item) => `${item.handoff_summary} ${item.ticket_id} ${item.queue_code}`.toLocaleLowerCase().includes(keyword)) : tickets.value
})
function isStoreSender(item: SupportTicket): boolean {
  const marker = `${item.queue_type} ${item.queue_code} ${item.ticket_type}`.toLocaleLowerCase()
  return marker.includes('store') || marker.includes('merchant')
}
const userTickets = computed(() => filteredTickets.value.filter((item) => !isStoreSender(item)))
const storeTickets = computed(() => filteredTickets.value.filter(isStoreSender))
const initials = computed(() => '管')

function token(): string { if (!auth.accessToken) throw new Error('管理会话不可用'); return auth.accessToken }
function dateTime(value: string): string { return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(new Date(value)) }
function messageText(message: ChatMessage): string { return message.text || (message.message_type === 'product_card' ? '[商品卡片]' : message.message_type === 'order_card' ? '[订单卡片]' : '[系统消息]') }
function senderName(message: ChatMessage): string { return ({ user: '用户', human: '平台客服', agent: 'AI 客服', system: '系统', tool: '工具' } as Record<string, string>)[message.sender_type] ?? message.sender_type }

async function loadTickets() {
  loading.value = true; error.value = ''
  try {
    tickets.value = (await listSupportTickets({}, token())).data.items
    emit('unread-change', tickets.value.reduce((total, item) => total + item.unread_count, 0))
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function selectTicket(ticket: SupportTicket) {
  selected.value = ticket.ticket_id; workspace.value = null; messages.value = []; error.value = ''; loading.value = true
  try {
    workspace.value = (await getSupportWorkspace(ticket.ticket_id, token())).data
    const current = workspace.value.ticket
    const mine = current.assigned_user_id === auth.userId
    if (mine && ['assigned', 'active', 'waiting_user'].includes(current.ticket_status)) {
      messages.value = (await listSupportMessages(current.conversation_id, token())).data.items
      const latest = [...messages.value].reverse().find((item) => item.sender_type === 'user')
      if (latest) await putSupportReadCursor(current.conversation_id, latest, token())
      ticket.unread_count = 0
      emit('unread-change', tickets.value.reduce((total, item) => total + item.unread_count, 0))
    }
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function claim() {
  if (!selectedTicket.value || busy.value) return
  busy.value = true; error.value = ''
  try {
    const result = await claimSupportTicket(selectedTicket.value, token())
    Object.assign(selectedTicket.value, result.data)
    await selectTicket(selectedTicket.value)
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}

async function send() {
  const text = reply.value.trim()
  if (!text || !selectedTicket.value || busy.value) return
  busy.value = true; error.value = ''; reply.value = ''
  try {
    const result = await sendSupportMessage(selectedTicket.value.conversation_id, text, token(), createClientMessageId())
    messages.value.push(result.data)
  } catch (cause) { reply.value = text; error.value = errorMessage(cause) }
  finally { busy.value = false }
}

function closeOnEscape(event: KeyboardEvent) { if (event.key === 'Escape') emit('close') }
onMounted(async () => { window.addEventListener('keydown', closeOnEscape); await loadTickets() })
onBeforeUnmount(() => window.removeEventListener('keydown', closeOnEscape))
</script>

<template>
  <div class="admin-message-overlay" @click.self="emit('close')">
    <section class="admin-message-window" role="dialog" aria-modal="true" aria-label="管理端消息中心">
      <aside class="admin-chat-sidebar">
        <header><div><strong>消息中心</strong><small>用户、店铺与 AI 管家</small></div><button aria-label="关闭消息中心" @click="emit('close')">×</button></header>
        <label class="admin-chat-search"><span>⌕</span><input v-model="search" placeholder="搜索会话" /></label>
        <div class="admin-chat-list">
          <button class="admin-chat-item ai" :class="{ active: selected === 'ai' }" @click="selected = 'ai'; workspace = null; messages = []"><span class="admin-chat-avatar ai">✦</span><span><strong>AI 管家</strong><small>管理工作助手 · 固定置顶</small></span><time>置顶</time></button>
          <section class="admin-chat-group"><button class="admin-chat-group-title" @click="userGroupOpen = !userGroupOpen"><span>{{ userGroupOpen ? '⌄' : '›' }} 用户消息</span><b>{{ userTickets.length }}</b></button><template v-if="userGroupOpen"><button v-for="ticket in userTickets" :key="ticket.ticket_id" class="admin-chat-item" :class="{ active: selected === ticket.ticket_id }" @click="selectTicket(ticket)"><span class="admin-chat-avatar">{{ (ticket.handoff_summary || '用').slice(0, 1) }}</span><span><strong>{{ ticket.handoff_summary || '用户咨询' }}</strong><small>{{ ticket.ticket_status === 'queued' ? '等待领取' : ticket.queue_code }}</small></span><b v-if="ticket.unread_count">{{ ticket.unread_count }}</b><time>{{ dateTime(ticket.updated_at) }}</time></button><p v-if="!userTickets.length">暂无用户会话</p></template></section>
          <section class="admin-chat-group"><button class="admin-chat-group-title" @click="storeGroupOpen = !storeGroupOpen"><span>{{ storeGroupOpen ? '⌄' : '›' }} 店铺消息</span><b>{{ storeTickets.length }}</b></button><template v-if="storeGroupOpen"><button v-for="ticket in storeTickets" :key="ticket.ticket_id" class="admin-chat-item" :class="{ active: selected === ticket.ticket_id }" @click="selectTicket(ticket)"><span class="admin-chat-avatar store">店</span><span><strong>{{ ticket.handoff_summary || '店铺咨询' }}</strong><small>{{ ticket.ticket_status === 'queued' ? '等待领取' : ticket.queue_code }}</small></span><b v-if="ticket.unread_count">{{ ticket.unread_count }}</b><time>{{ dateTime(ticket.updated_at) }}</time></button><p v-if="!storeTickets.length">暂无店铺会话</p></template></section>
        </div>
      </aside>

      <main class="admin-chat-main">
        <template v-if="selected === 'ai'">
          <header class="admin-chat-header"><div><strong>AI 管家</strong><small><span />在线 · 默认只读</small></div></header>
          <section class="admin-ai-concierge">
            <div class="admin-ai-orb">✦</div><p class="eyebrow">ADMIN COPILOT</p><h2>今天想先处理什么？</h2><p>我可以帮助你快速定位用户、店铺、订单和 AI 运行问题。涉及资金、冻结、删除或发布的操作仍会要求人工确认。</p>
            <div class="admin-ai-suggestions"><RouterLink to="/admin/users"><span>♙</span><strong>查看异常用户</strong><small>账号状态与登录会话</small></RouterLink><RouterLink to="/admin/orders"><span>▤</span><strong>检查异常订单</strong><small>支付、履约与售后</small></RouterLink><RouterLink to="/admin/observability"><span>⌇</span><strong>分析 AI 告警</strong><small>延迟、错误与工具调用</small></RouterLink><RouterLink to="/admin/approval-requests"><span>✓</span><strong>查看待办审批</strong><small>高风险操作复核</small></RouterLink></div>
            <div class="alert info">AI 管家的自然语言执行能力将在受控 Agent Runtime 接入；当前入口先提供安全的只读导航与诊断捷径，不会伪造执行结果。</div>
          </section>
        </template>

        <template v-else>
          <header class="admin-chat-header"><div><strong>{{ selectedTicket?.queue_type === 'store' ? '店铺专属客服' : '用户专属客服' }}</strong><small>{{ selectedTicket?.ticket_id }} · {{ selectedTicket?.ticket_status }}</small></div><RouterLink v-if="selectedTicket" :to="`/admin/support/tickets/${selectedTicket.ticket_id}`" @click="emit('close')">完整工作台 ↗</RouterLink></header>
          <p v-if="error" class="alert error">{{ error }}</p>
          <div v-if="loading" class="admin-chat-loading">正在读取会话…</div>
          <section v-else-if="workspace" class="admin-chat-conversation">
            <div v-if="!assignedToMe" class="admin-chat-claim"><span>◍</span><h2>{{ selectedTicket?.ticket_status === 'queued' ? '这条会话正在等待客服' : '会话由其他客服处理' }}</h2><p>领取后才能读取完整聊天内容并向对方回复，避免多个管理员同时处理。</p><button v-if="selectedTicket?.ticket_status === 'queued' && auth.has('support:claim')" :disabled="busy" @click="claim">领取并开始处理</button></div>
            <template v-else>
              <div class="admin-chat-timeline"><article v-for="message in messages" :key="message.message_id" :class="{ mine: message.sender_type === 'human' }"><span class="admin-chat-bubble-avatar">{{ message.sender_type === 'human' ? initials : selectedTicket?.queue_type === 'store' ? '店' : '用' }}</span><div><strong>{{ senderName(message) }}</strong><p>{{ messageText(message) }}</p><time>{{ dateTime(message.sent_at) }}</time></div></article><p v-if="!messages.length" class="empty-state">暂无聊天消息</p></div>
              <form class="admin-chat-composer" @submit.prevent="send"><textarea v-model="reply" maxlength="4000" :disabled="!canChat" :placeholder="canChat ? '输入回复，Enter 换行，点击发送提交' : '当前会话状态暂不可回复'" /><div><span>回复会对用户或店铺立即可见</span><button :disabled="!canChat || !reply.trim() || busy">发送</button></div></form>
            </template>
          </section>
        </template>
      </main>
    </section>
  </div>
</template>
