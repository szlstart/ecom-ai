<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { listSupportTickets, type SupportTicket, type SupportTicketStatus } from '@/api/admin-support'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const items = ref<SupportTicket[]>([])
const loading = ref(true)
const error = ref('')
const status = ref<SupportTicketStatus | ''>('')
function statusLabel(value: string) { return ({ queued: '待领取', assigned: '待接受', active: '沟通中', waiting_user: '等待用户', resolved: '已解决', closed: '已关闭' } as Record<string, string>)[value] ?? value }
async function load() { loading.value = true; error.value = ''; try { items.value = (await listSupportTickets({ queueType: 'store', status: status.value || undefined }, auth.accessToken!)).data.items } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false } }
onMounted(load)
</script>

<template>
  <section class="merchant-page-stack">
    <header class="merchant-page-heading"><div><p class="eyebrow">店铺客服</p><h1>客户咨询</h1><p>这里只显示已转交给当前店铺人工处理的咨询。</p></div><button class="secondary" :disabled="loading" @click="load">刷新</button></header>
    <div class="merchant-segmented"><button :class="{ active: status === '' }" @click="status = ''; load()">全部</button><button :class="{ active: status === 'queued' }" @click="status = 'queued'; load()">待领取</button><button :class="{ active: status === 'active' }" @click="status = 'active'; load()">沟通中</button><button :class="{ active: status === 'waiting_user' }" @click="status = 'waiting_user'; load()">等待用户</button><button :class="{ active: status === 'resolved' }" @click="status = 'resolved'; load()">已解决</button></div>
    <PageState :loading="loading" :error="error" :empty="!loading && !error && items.length === 0" empty-title="当前没有客户咨询" @retry="load"><div class="merchant-ticket-list"><RouterLink v-for="item in items" :key="item.ticket_id" :to="`/merchant/support/${item.ticket_id}`" class="card merchant-ticket-card"><div><span class="badge">{{ statusLabel(item.ticket_status) }}</span><strong>{{ item.handoff_summary || '用户需要店铺人工协助' }}</strong><small>{{ item.ticket_type }} · {{ item.priority === 'urgent' ? '紧急' : item.priority === 'high' ? '高优先级' : '普通优先级' }}</small></div><div><time>{{ new Date(item.updated_at).toLocaleString('zh-CN') }}</time><span>进入会话 →</span></div></RouterLink></div></PageState>
  </section>
</template>
