<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'

import {
  claimSupportTicket,
  getSupportWorkspace,
  listSupportMessages,
  listSupportTickets,
  sendSupportMessage,
  type SupportTicket,
  type SupportWorkspace,
} from '@/api/admin-support'
import { errorMessage } from '@/api/http'
import {
  ensureMerchantHumanService,
  getMerchantExclusiveConversation,
  listMerchantExclusiveMessages,
  sendMerchantExclusiveMessage,
} from '@/api/merchant-support'
import type { ChatMessage } from '@/api/messaging'
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
const timeline = ref<HTMLElement | null>(null)

const activeTicket = computed(() => tickets.value.find((item) => item.ticket_id === selectedKey.value) ?? null)
const activeMessages = computed(() => selectedKey.value === 'exclusive' ? exclusiveMessages.value : messages.value)
const unresolvedCount = computed(() => tickets.value.filter((item) => !['resolved', 'closed'].includes(item.ticket_status)).length)
const title = computed(() => selectedKey.value === 'exclusive' ? '专属客服' : workspace.value?.user.nickname || '顾客咨询')
const subtitle = computed(() => {
  if (selectedKey.value === 'exclusive') return '平台商家支持 · 工作日优先响应'
  return activeTicket.value ? statusLabel(activeTicket.value.ticket_status) : '顾客咨询'
})

function token() { return auth.accessToken! }
function statusLabel(value: string) {
  return ({ queued: '等待接待', assigned: '已分配', active: '正在沟通', waiting_user: '等待顾客', resolved: '已解决', closed: '已关闭' } as Record<string, string>)[value] ?? value
}
function messageText(item: ChatMessage) { return item.text || (item.message_type === 'product_card' ? '[商品卡片]' : item.message_type === 'order_card' ? '[订单卡片]' : '[系统消息]') }
function isMine(item: ChatMessage) {
  return selectedKey.value === 'exclusive' ? item.sender_type === 'user' : item.sender_type === 'human'
}
async function scrollBottom() { await nextTick(); timeline.value?.scrollTo({ top: timeline.value.scrollHeight }) }

async function loadTickets() {
  try { tickets.value = (await listSupportTickets({ queueType: 'store' }, token())).data.items }
  catch (cause) { error.value = errorMessage(cause) }
}

async function loadExclusive() {
  loading.value = true; error.value = ''
  try {
    await getMerchantExclusiveConversation(token())
    exclusiveMessages.value = (await listMerchantExclusiveMessages(token())).data.items
    await scrollBottom()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function selectConversation(key: string) {
  selectedKey.value = key; workspace.value = null; messages.value = []; error.value = ''
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
    await scrollBottom()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function show() {
  open.value = true
  await Promise.all([loadTickets(), loadExclusive()])
}

async function send() {
  const text = draft.value.trim()
  if (!text || sending.value) return
  sending.value = true; error.value = ''
  try {
    if (selectedKey.value === 'exclusive') {
      const sent = (await sendMerchantExclusiveMessage(text, token())).data
      exclusiveMessages.value.push(sent)
      await ensureMerchantHumanService(text, sent.message_id, token())
    } else if (activeTicket.value) {
      messages.value.push((await sendSupportMessage(activeTicket.value.conversation_id, text, token())).data)
    }
    draft.value = ''
    await scrollBottom()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { sending.value = false }
}

onMounted(loadTickets)
</script>

<template>
  <button class="merchant-message-trigger" type="button" aria-haspopup="dialog" @click="show">
    <span aria-hidden="true">💬</span><span>消息</span><b v-if="unresolvedCount">{{ unresolvedCount > 99 ? '99+' : unresolvedCount }}</b>
  </button>
  <Teleport to="body">
    <div v-if="open" class="merchant-message-overlay" @mousedown.self="open = false">
      <section class="merchant-message-window" role="dialog" aria-modal="true" aria-label="商家消息中心">
        <aside class="merchant-chat-list">
          <header><div><strong>消息</strong><small>{{ tickets.length }} 位顾客</small></div><button type="button" aria-label="关闭消息中心" @click="open = false">×</button></header>
          <button class="merchant-chat-item pinned" :class="{ active: selectedKey === 'exclusive' }" type="button" @click="selectConversation('exclusive')">
            <span class="merchant-chat-avatar platform">专</span><span><strong>专属客服 <em>置顶</em></strong><small>平台商家支持</small></span>
          </button>
          <p v-if="!tickets.length" class="merchant-chat-empty">暂时没有顾客咨询</p>
          <button v-for="ticket in tickets" :key="ticket.ticket_id" class="merchant-chat-item" :class="{ active: selectedKey === ticket.ticket_id }" type="button" @click="selectConversation(ticket.ticket_id)">
            <span class="merchant-chat-avatar">客</span><span><strong>{{ ticket.handoff_summary || '顾客咨询' }}</strong><small>{{ statusLabel(ticket.ticket_status) }} · {{ new Date(ticket.updated_at).toLocaleString('zh-CN') }}</small></span><i v-if="!['resolved','closed'].includes(ticket.ticket_status)"></i>
          </button>
        </aside>
        <main class="merchant-chat-main">
          <header><div><strong>{{ title }}</strong><small>{{ subtitle }}</small></div></header>
          <p v-if="error" class="merchant-chat-error">{{ error }}</p>
          <div ref="timeline" class="merchant-chat-timeline">
            <div v-if="selectedKey === 'exclusive' && !activeMessages.length && !loading" class="merchant-chat-welcome"><span class="merchant-chat-avatar platform">专</span><h2>你好，我是你的专属客服</h2><p>店铺经营、商品审核、订单履约或平台规则方面遇到问题，都可以直接在这里留言。消息会进入平台商家支持队列。</p></div>
            <p v-if="loading" class="merchant-chat-loading">正在读取消息…</p>
            <article v-for="item in activeMessages" :key="item.message_id" class="merchant-chat-bubble-row" :class="{ mine: isMine(item) }"><span v-if="!isMine(item)" class="merchant-chat-avatar">{{ selectedKey === 'exclusive' ? '专' : '客' }}</span><div><p>{{ messageText(item) }}</p><time>{{ new Date(item.sent_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }}</time></div></article>
          </div>
          <form class="merchant-chat-composer" @submit.prevent="send"><textarea v-model="draft" rows="3" maxlength="4000" :placeholder="selectedKey === 'exclusive' ? '向平台专属客服描述你的问题…' : '回复顾客…'" @keydown.enter.exact.prevent="send" /><footer><small>Enter 发送 · Shift + Enter 换行</small><button :disabled="sending || !draft.trim()">{{ sending ? '发送中…' : '发送' }}</button></footer></form>
        </main>
      </section>
    </div>
  </Teleport>
</template>
