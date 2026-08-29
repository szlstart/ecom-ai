<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { errorMessage } from '@/api/http'
import { RealtimeConnection, type RealtimeEvent, type RealtimeState } from '@/api/realtime'
import {
  archiveConversation,
  ensureExclusiveConversation,
  listConversations,
  type Conversation,
} from '@/api/messaging'
import PageState from '@/components/PageState.vue'
import { confirmAction } from '@/composables/confirmation'
import { useUserAuthStore } from '@/stores/user-auth'

const auth = useUserAuthStore()
const items = ref<Conversation[]>([])
const loading = ref(true)
const error = ref('')
const busyId = ref('')
const openMenuId = ref('')
const connectionState = ref<RealtimeState>('polling')
const exclusive = computed(() => items.value.find((item) => item.conversation_type === 'exclusive') ?? null)
const stores = computed(() => items.value.filter((item) => item.conversation_type === 'store'))
let realtime: RealtimeConnection | undefined
let refreshTimer: number | undefined

function token(): string {
  if (!auth.accessToken) throw new Error('missing user token')
  return auth.accessToken
}
function timeLabel(value: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  const today = new Date()
  if (date.toDateString() === today.toDateString()) return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit' }).format(date)
  const yesterday = new Date(today)
  yesterday.setDate(today.getDate() - 1)
  if (date.toDateString() === yesterday.toDateString()) return '昨天'
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit' }).format(date)
}
function unreadLabel(value: number): string { return value > 99 ? '99+' : String(value) }
async function load() {
  loading.value = true
  error.value = ''
  try {
    await ensureExclusiveConversation(token())
    items.value = (await listConversations(token())).data.items
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}
async function refreshQuietly() {
  try { items.value = (await listConversations(token())).data.items }
  catch { connectionState.value = navigator.onLine ? 'polling' : 'offline' }
}
function handleRealtime(event: RealtimeEvent) {
  if (!['message.created', 'unread.updated', 'support.status.updated'].includes(event.type)) return
  if (refreshTimer) return
  refreshTimer = window.setTimeout(() => { refreshTimer = undefined; void refreshQuietly() }, 100)
}
function toggleMenu(conversationId: string) { openMenuId.value = openMenuId.value === conversationId ? '' : conversationId }
async function archive(item: Conversation) {
  openMenuId.value = ''
  if (!await confirmAction('确认从列表隐藏该会话吗？重新咨询或收到新消息时，该会话会恢复显示。')) return
  busyId.value = item.conversation_id
  error.value = ''
  try {
    await archiveConversation(item.conversation_id, item.version, token())
    items.value = items.value.filter((candidate) => candidate.conversation_id !== item.conversation_id)
  } catch (cause) { error.value = errorMessage(cause); await load() }
  finally { busyId.value = '' }
}
onMounted(async () => {
  await load()
  realtime = new RealtimeConnection({
    audience: 'user', token, onEvent: handleRealtime,
    onState: (state) => { connectionState.value = state },
    beforeReconnect: refreshQuietly,
  })
  realtime.start()
})
onBeforeUnmount(() => {
  realtime?.stop()
  if (refreshTimer) window.clearTimeout(refreshTimer)
})
</script>

<template>
  <section class="message-list-page" @keydown.esc="openMenuId = ''">
    <header class="page-heading">
      <div><p class="eyebrow">服务消息</p><h1>消息</h1><p class="muted"><span class="connection-dot" :class="connectionState" />消息进入可见区域后才会标记为已读。</p></div>
      <button type="button" class="secondary" :disabled="loading" @click="load">刷新</button>
    </header>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <PageState :loading="loading" :error="''" :empty="!exclusive" empty-title="暂时无法加载专属客服" @retry="load">
      <div v-if="exclusive" class="conversation-sections">
        <section aria-labelledby="exclusive-heading">
          <h2 id="exclusive-heading" class="conversation-section-title">专属客服</h2>
          <RouterLink class="conversation-row fixed" :to="`/messages/${exclusive.conversation_id}`">
            <span class="conversation-avatar platform" aria-hidden="true">AI</span>
            <span class="conversation-copy"><strong>{{ exclusive.title }}</strong><small>{{ exclusive.last_message_preview || '有什么可以帮您？' }}</small></span>
            <span class="conversation-meta"><span class="badge">固定置顶</span><span v-if="exclusive.unread_count" class="unread-badge" :aria-label="`${exclusive.unread_count} 条未读消息`">{{ unreadLabel(exclusive.unread_count) }}</span></span>
          </RouterLink>
        </section>
        <section aria-labelledby="store-heading">
          <h2 id="store-heading" class="conversation-section-title">店铺会话</h2>
          <p v-if="stores.length === 0" class="conversation-empty">暂无店铺会话，去商品页咨询吧～</p>
          <article v-for="item in stores" :key="item.conversation_id" class="conversation-row-wrap" @contextmenu.prevent="openMenuId = item.conversation_id">
            <RouterLink class="conversation-row" :to="`/messages/${item.conversation_id}`">
              <span class="conversation-avatar" aria-hidden="true">{{ item.title.slice(0, 1) }}</span>
              <span class="conversation-copy"><strong>{{ item.title }}</strong><small>{{ item.last_message_preview || '开始咨询店铺客服' }}</small></span>
              <span class="conversation-meta"><time v-if="item.last_message_at" :datetime="item.last_message_at">{{ timeLabel(item.last_message_at) }}</time><span v-if="item.unread_count" class="unread-badge" :aria-label="`${item.unread_count} 条未读消息`">{{ unreadLabel(item.unread_count) }}</span></span>
            </RouterLink>
            <button type="button" class="conversation-menu-button" :aria-expanded="openMenuId === item.conversation_id" :aria-controls="`conversation-menu-${item.conversation_id}`" aria-label="更多会话操作" @click="toggleMenu(item.conversation_id)">…</button>
            <div v-if="openMenuId === item.conversation_id" :id="`conversation-menu-${item.conversation_id}`" class="conversation-menu" role="menu">
              <RouterLink role="menuitem" :to="`/stores/${item.store_id}`" @click="openMenuId = ''">查看店铺</RouterLink>
              <button role="menuitem" type="button" :disabled="busyId === item.conversation_id" @click="archive(item)">{{ busyId === item.conversation_id ? '隐藏中…' : '从列表隐藏会话' }}</button>
            </div>
          </article>
        </section>
      </div>
    </PageState>
  </section>
</template>
