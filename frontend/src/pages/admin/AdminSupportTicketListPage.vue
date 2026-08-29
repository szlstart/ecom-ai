<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { listSupportTickets, type SupportTicket, type SupportTicketStatus } from '@/api/admin-support'
import { errorMessage } from '@/api/http'
import { RealtimeConnection, type RealtimeEvent, type RealtimeState } from '@/api/realtime'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const items = ref<SupportTicket[]>([])
const loading = ref(true)
const error = ref('')
const queueType = ref('')
const status = ref<SupportTicketStatus | ''>('')
const connectionState = ref<RealtimeState>('polling')
let realtime: RealtimeConnection | undefined
let refreshTimer: number | undefined
const statuses: Array<{ value: SupportTicketStatus | ''; label: string }> = [
  { value: '', label: '处理中' }, { value: 'queued', label: '待领取' }, { value: 'active', label: '处理中' },
  { value: 'waiting_user', label: '等待用户' }, { value: 'resolved', label: '已解决' },
]

function token(): string { if (!auth.accessToken) throw new Error('missing admin token'); return auth.accessToken }
function statusLabel(value: string): string { return ({ queued: '待领取', assigned: '待接受', active: '处理中', waiting_user: '等待用户', resolved: '已解决', closed: '已关闭' } as Record<string, string>)[value] ?? value }
function dateTime(value: string): string { return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(value)) }
async function load() {
  loading.value = true; error.value = ''
  try { items.value = (await listSupportTickets({ queueType: queueType.value || undefined, status: status.value || undefined }, token())).data.items }
  catch (cause) { error.value = errorMessage(cause); items.value = [] }
  finally { loading.value = false }
}
async function refreshQuietly() {
  try { items.value = (await listSupportTickets({ queueType: queueType.value || undefined, status: status.value || undefined }, token())).data.items }
  catch { connectionState.value = navigator.onLine ? 'polling' : 'offline' }
}
function handleRealtime(event: RealtimeEvent) {
  if (event.type !== 'support.ticket.updated' || refreshTimer) return
  refreshTimer = window.setTimeout(() => { refreshTimer = undefined; void refreshQuietly() }, 100)
}
onMounted(async () => {
  await load()
  realtime = new RealtimeConnection({
    audience: 'admin', token, onEvent: handleRealtime,
    onState: (state) => { connectionState.value = state },
    beforeReconnect: refreshQuietly,
  })
  realtime.start()
})
onBeforeUnmount(() => { realtime?.stop(); if (refreshTimer) window.clearTimeout(refreshTimer) })
</script>

<template>
  <section class="admin-page-stack">
    <header class="page-heading"><div><p class="eyebrow">消息与客服</p><h1>人工客服队列</h1><p class="muted"><span class="connection-dot" :class="connectionState" />平台与店铺工单按当前管理身份的数据范围隔离。</p></div><button type="button" class="secondary" :disabled="loading" @click="load">刷新</button></header>
    <form class="filter-bar" @submit.prevent="load">
      <label>队列类型<select v-model="queueType"><option value="">全部可见队列</option><option value="platform">平台客服</option><option value="store">店铺客服</option></select></label>
      <label>工单状态<select v-model="status"><option v-for="item in statuses" :key="item.value || 'processing'" :value="item.value">{{ item.label }}</option></select></label>
      <button>筛选</button>
    </form>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <PageState :loading="loading" :error="''" :empty="items.length === 0" empty-title="当前队列没有工单" @retry="load">
      <div class="table-wrap"><table><thead><tr><th>工单</th><th>队列</th><th>状态</th><th>交接摘要</th><th>SLA</th><th>操作</th></tr></thead><tbody>
        <tr v-for="item in items" :key="item.ticket_id"><td><strong>{{ item.ticket_id }}</strong><small>{{ item.ticket_type }} · {{ item.priority }}</small></td><td>{{ item.queue_type === 'platform' ? '平台' : '店铺' }}<small>{{ item.queue_code }}</small></td><td><span class="badge">{{ statusLabel(item.ticket_status) }}</span><small v-if="item.assigned_user_id">客服 {{ item.assigned_user_id }}</small></td><td class="support-summary">{{ item.handoff_summary }}</td><td>{{ item.sla_due_at ? dateTime(item.sla_due_at) : item.ticket_status === 'waiting_user' ? '已暂停' : '—' }}</td><td><RouterLink :to="`/admin/support/tickets/${item.ticket_id}`">进入工作台</RouterLink></td></tr>
      </tbody></table></div>
    </PageState>
  </section>
</template>
