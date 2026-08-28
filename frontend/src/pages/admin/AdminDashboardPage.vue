<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { listSupportTickets, type SupportTicket } from '@/api/admin-support'
import { adminGet, type AdminProductSummary, type AdminStore } from '@/api/admin-catalog'
import { apiRequest, errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'

interface Summary { generated_at: string; active_user_count: number | null; pending_approval_count: number }
interface UserList { items: Array<{ user_id: string; account_status: string; registered_at: string }> }

const auth = useAdminAuthStore()
const summary = ref<Summary | null>(null)
const users = ref<UserList['items']>([])
const stores = ref<AdminStore[]>([])
const products = ref<AdminProductSummary[]>([])
const tickets = ref<SupportTicket[]>([])
const loading = ref(true)
const error = ref('')
const scopeText = computed(() => auth.scopes.some((item) => item.scope_type === 'platform') ? '平台全局视图' : `${auth.scopes.length} 个授权范围`)
const todayText = new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }).format(new Date())
const newUsersToday = computed(() => users.value.filter((item) => new Date(item.registered_at).toDateString() === new Date().toDateString()).length)
const pendingStores = computed(() => stores.value.filter((item) => item.status === 'pending').length)
const riskyProducts = computed(() => products.value.filter((item) => ['pending_review', 'rejected', 'needs_changes'].includes(item.status)).length)
const pendingTickets = computed(() => tickets.value.filter((item) => ['queued', 'assigned', 'active'].includes(item.ticket_status)).length)
const unreadMessages = computed(() => tickets.value.reduce((total, item) => total + item.unread_count, 0))

async function load() {
  loading.value = true; error.value = ''
  try {
    summary.value = (await apiRequest<Summary>('/admin/dashboard', {}, auth.accessToken)).data
    const tasks: Array<Promise<void>> = []
    if (auth.has('users:read')) tasks.push(apiRequest<UserList>('/admin/users?limit=100', {}, auth.accessToken).then((result) => { users.value = result.data.items }))
    if (auth.has('stores:read')) tasks.push(adminGet<{ items: AdminStore[] }>('/admin/stores?limit=100', auth.accessToken!).then((result) => { stores.value = result.data.items }))
    if (auth.has('products:read')) tasks.push(adminGet<{ items: AdminProductSummary[] }>('/admin/products?limit=100', auth.accessToken!).then((result) => { products.value = result.data.items }))
    if (auth.has('support:queue_read')) tasks.push(listSupportTickets({}, auth.accessToken!).then((result) => { tickets.value = result.data.items }))
    await Promise.allSettled(tasks)
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

onMounted(load)
</script>

<template>
  <section class="admin-page-stack admin-dashboard-premium">
    <header class="admin-dashboard-hero">
      <div><p class="eyebrow">{{ scopeText }} · {{ todayText }}</p><h1>欢迎回来，今天从这里开始。</h1><p>用户、店铺、交易和 AI 服务的重要变化集中在一个工作区，不需要在几十个菜单之间来回寻找。</p></div>
      <div class="admin-dashboard-hero-actions"><button class="secondary" :disabled="loading" @click="load">{{ loading ? '正在更新…' : '刷新数据' }}</button><RouterLink v-if="auth.has('admin_approvals:read')" class="button-link" to="/admin/approval-requests">处理待办审批</RouterLink></div>
    </header>
    <p v-if="error" class="alert error">{{ error }}</p>

    <div class="admin-metric-grid">
      <RouterLink v-if="auth.has('users:read')" to="/admin/users" class="admin-metric-card blue"><span>♙</span><div><small>有效用户</small><strong>{{ summary?.active_user_count ?? users.length }}</strong><p>今日新增 {{ newUsersToday }} 人</p></div><b>↗</b></RouterLink>
      <RouterLink v-if="auth.has('stores:read')" to="/admin/stores" class="admin-metric-card green"><span>▣</span><div><small>当前可见店铺</small><strong>{{ stores.length }}</strong><p>{{ pendingStores }} 家需要关注</p></div><b>↗</b></RouterLink>
      <RouterLink v-if="auth.has('products:read')" to="/admin/stores" class="admin-metric-card purple"><span>▦</span><div><small>当前可见商品</small><strong>{{ products.length }}</strong><p>{{ riskyProducts }} 件待处理 · 从店铺进入</p></div><b>↗</b></RouterLink>
      <RouterLink v-if="auth.has('support:queue_read')" to="/admin/support/tickets" class="admin-metric-card orange"><span>◍</span><div><small>客服待处理</small><strong>{{ pendingTickets }}</strong><p>{{ unreadMessages }} 条未读消息</p></div><b>↗</b></RouterLink>
    </div>

    <div class="admin-dashboard-grid">
      <article class="admin-panel admin-priority-panel">
        <header><div><p class="eyebrow">TODAY</p><h2>今日优先处理</h2></div><span>按风险排序</span></header>
        <div class="admin-priority-list">
          <RouterLink v-if="summary?.pending_approval_count" to="/admin/approval-requests"><span class="warning">!</span><div><strong>{{ summary.pending_approval_count }} 项高风险操作等待复核</strong><small>审批超时前需要独立管理员完成决定</small></div><b>立即处理</b></RouterLink>
          <RouterLink v-if="pendingTickets" to="/admin/support/tickets"><span class="message">◍</span><div><strong>{{ pendingTickets }} 个用户或店铺会话待处理</strong><small>顶部“消息”可以直接打开微信式工作台</small></div><b>打开消息</b></RouterLink>
          <RouterLink v-if="riskyProducts" to="/admin/stores"><span class="product">▦</span><div><strong>{{ riskyProducts }} 件商品需要查看</strong><small>先选择所属店铺，再处理审核和异常状态</small></div><b>选择店铺</b></RouterLink>
          <div v-if="!summary?.pending_approval_count && !pendingTickets && !riskyProducts" class="admin-all-clear"><span>✓</span><div><strong>当前没有紧急待办</strong><small>系统状态平稳，可以继续日常巡检。</small></div></div>
        </div>
      </article>

      <article class="admin-panel admin-ai-status-panel">
        <header><div><p class="eyebrow">AI CONTROL</p><h2>AI 智能中心</h2></div><span class="healthy"><i />运行中</span></header>
        <div class="admin-ai-status-visual"><span>✦</span><div><strong>Agent、MCP、Skill、RAG</strong><small>版本化配置和权限边界集中治理</small></div></div>
        <nav><RouterLink v-if="auth.has('ai_agents:read')" to="/admin/ai/agents">Agent 管理 <b>→</b></RouterLink><RouterLink v-if="auth.has('ai_tools:read')" to="/admin/ai/tools">MCP 工具 <b>→</b></RouterLink><RouterLink v-if="auth.has('ai_skills:read')" to="/admin/ai/skills">Skill 管理 <b>→</b></RouterLink><RouterLink v-if="auth.has('knowledge:read')" to="/admin/knowledge/documents">RAG 知识库 <b>→</b></RouterLink></nav>
      </article>
    </div>

    <article class="admin-panel admin-quick-panel"><header><div><p class="eyebrow">QUICK ACCESS</p><h2>常用管理入口</h2></div></header><div class="admin-quick-grid"><RouterLink v-if="auth.has('users:read')" to="/admin/users"><span>♙</span><strong>管理用户</strong><small>资料、安全与交易关系</small></RouterLink><RouterLink v-if="auth.has('stores:read')" to="/admin/stores"><span>▣</span><strong>管理店铺</strong><small>资料、商品与经营状态</small></RouterLink><RouterLink v-if="auth.has('orders:read')" to="/admin/stores"><span>▤</span><strong>店铺订单监管</strong><small>选择店铺后查看支付、履约与售后</small></RouterLink><RouterLink v-if="auth.has('audit:read')" to="/admin/audit-logs"><span>◎</span><strong>审计追踪</strong><small>查看所有管理操作</small></RouterLink></div></article>
  </section>
</template>
