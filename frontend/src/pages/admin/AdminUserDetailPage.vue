<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import {
  adjustAdminUserWallet,
  deleteAdminUser,
  replaceAdminUserPassword,
  updateAdminUser,
  type AdminUserSummary,
} from '@/api/admin-users'
import { ApiProblem, apiRequest, createIdempotencyKey, errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

interface Grant { grant_id: string; role_id: string; role_name: string; scope_type: string; scope_id: number; status: string; version: number }
interface TimelineEvent { event_id?: string; status_event_id?: string; event_type?: string; to_status?: string; reason: string; created_at?: string; effective_at?: string }
type Tab = 'overview' | 'profile' | 'security' | 'permissions' | 'activity'

const route = useRoute()
const router = useRouter()
const auth = useAdminAuthStore()
const userId = String(route.params.userId)
const user = ref<AdminUserSummary | null>(null)
const grants = ref<Grant[]>([])
const statusEvents = ref<TimelineEvent[]>([])
const grantEvents = ref<TimelineEvent[]>([])
const etag = ref('')
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const tab = ref<Tab>('overview')
const reason = ref('')
const profile = reactive({ username: '', nickname: '', email: '' })
const password = reactive({ temporary_password: '', require_change_on_next_login: true, reason: '' })
const wallet = reactive<{ direction: 'credit' | 'debit'; amount_yuan: number | null; reason: string }>({ direction: 'credit', amount_yuan: null, reason: '' })
const walletResult = ref<{ balance_minor: string; currency: string } | null>(null)
const deleteOpen = ref(false)
const deleteReason = ref('')
const deleteConfirmation = ref('')
const fieldErrors = ref<Record<string, string>>({})
const sensitiveGrant = ref('')
const sensitiveGrantEtag = ref('')
const sensitive = ref<Record<string, string> | null>(null)

const initials = computed(() => (user.value?.nickname || user.value?.username || '用').slice(0, 1).toUpperCase())
const activeGrantCount = computed(() => grants.value.filter((item) => item.status === 'active').length)
function statusLabel(value: string): string { return ({ active: '正常使用', suspended: '已冻结', closed: '已注销' } as Record<string, string>)[value] ?? value }
function dateTime(value: string | null | undefined): string { return value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : '—' }

async function load() {
  loading.value = true; error.value = ''
  try {
    const userResult = await apiRequest<AdminUserSummary>(`/admin/users/${userId}`, {}, auth.accessToken)
    user.value = userResult.data; etag.value = userResult.headers.get('etag') ?? `"v${user.value.version}"`
    Object.assign(profile, { username: user.value.username, nickname: user.value.nickname, email: '' })
    const tasks: Array<Promise<void>> = []
    if (auth.has('rbac:read')) tasks.push(apiRequest<Grant[]>(`/admin/users/${userId}/role-grants`, {}, auth.accessToken).then((result) => { grants.value = result.data }))
    tasks.push(apiRequest<TimelineEvent[]>(`/admin/users/${userId}/status-events`, {}, auth.accessToken).then((result) => { statusEvents.value = result.data }))
    if (auth.has('rbac:read')) tasks.push(apiRequest<TimelineEvent[]>(`/admin/users/${userId}/role-grant-events`, {}, auth.accessToken).then((result) => { grantEvents.value = result.data }))
    await Promise.allSettled(tasks)
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function run(action: () => Promise<unknown>, success: string, reload = true) {
  busy.value = true; error.value = ''; notice.value = ''; fieldErrors.value = {}
  try { await action(); notice.value = success; if (reload) await load() }
  catch (cause) { if (cause instanceof ApiProblem) for (const item of cause.body.errors ?? []) fieldErrors.value[item.pointer.replace('/', '')] = item.message; error.value = errorMessage(cause) }
  finally { busy.value = false }
}

async function saveProfile() {
  if (!user.value) return
  const payload: { username?: string; nickname?: string; email?: string } = {}
  if (profile.username !== user.value.username) payload.username = profile.username
  if (profile.nickname !== user.value.nickname) payload.nickname = profile.nickname
  if (profile.email.trim()) payload.email = profile.email.trim()
  if (!Object.keys(payload).length) { error.value = '请先修改至少一项资料。'; return }
  await run(() => updateAdminUser(userId, payload, user.value!.version, auth.accessToken!), '用户资料已保存。')
}

async function changeStatus(action: 'suspend' | 'resume') {
  if (!user.value || reason.value.trim().length < 2) { error.value = '请先填写至少 2 个字的操作原因。'; return }
  await run(() => apiRequest(`/admin/users/${userId}/status-changes`, { method: 'POST', headers: { 'If-Match': etag.value, 'Idempotency-Key': createIdempotencyKey('user-status') }, body: JSON.stringify({ action, reason_code: 'ADMIN_DECISION', reason: reason.value.trim(), expires_at: null }) }, auth.accessToken), action === 'suspend' ? '用户已冻结并退出所有设备。' : '用户账号已恢复。')
}

async function revokeSessions() {
  await run(() => apiRequest(`/admin/users/${userId}/session-revocations`, { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('session-revoke') }, body: JSON.stringify({ scope: 'all', reason: reason.value.trim() || '管理员执行安全下线' }) }, auth.accessToken), '用户已从所有设备强制下线。', false)
}

async function savePassword() {
  if (!password.reason.trim()) { error.value = '请填写重置密码的原因。'; return }
  await run(() => replaceAdminUserPassword(userId, password, auth.accessToken!), '临时密码已设置，用户已从所有设备退出。', false)
  password.temporary_password = ''; password.reason = ''
}

async function adjustWallet() {
  if (!wallet.amount_yuan || wallet.amount_yuan <= 0 || !wallet.reason.trim()) { error.value = '请输入大于 0 的金额并填写调整原因。'; return }
  const amountMinor = Math.round(wallet.amount_yuan * 100)
  await run(async () => { walletResult.value = (await adjustAdminUserWallet(userId, { direction: wallet.direction, amount_minor: amountMinor, reason: wallet.reason.trim() }, auth.accessToken!)).data }, '余额调整已完成并生成不可修改的资金流水。', false)
  wallet.amount_yuan = null; wallet.reason = ''
}

async function createSensitiveGrant() {
  await run(async () => { const result = await apiRequest<{ grant_id: string }>(`/admin/users/${userId}/sensitive-field-access-grants`, { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('sensitive') }, body: JSON.stringify({ fields: ['email'], purpose_code: 'user_support', reason: reason.value.trim() || '核对用户账号资料', ttl_seconds: 300 }) }, auth.accessToken); sensitiveGrant.value = result.data.grant_id; sensitiveGrantEtag.value = result.headers.get('etag') ?? '' }, '已创建 5 分钟有效的一次性查看凭据。', false)
}
async function reveal() {
  await run(async () => { sensitive.value = (await apiRequest<{ values: Record<string, string> }>(`/admin/users/${userId}/sensitive-fields`, { headers: { 'X-Sensitive-Access-Grant': sensitiveGrant.value } }, auth.accessToken)).data.values; sensitiveGrant.value = ''; sensitiveGrantEtag.value = '' }, '敏感信息已读取，本次凭据已自动失效。', false)
}

async function removeUser() {
  if (!user.value || deleteConfirmation.value !== 'DELETE_USER' || deleteReason.value.trim().length < 2) return
  busy.value = true; error.value = ''
  try { await deleteAdminUser(userId, user.value.version, deleteReason.value.trim(), auth.accessToken!); await router.replace({ path: '/admin/users', query: { deleted: userId } }) }
  catch (cause) { error.value = errorMessage(cause); deleteOpen.value = false }
  finally { busy.value = false }
}

onMounted(load)
</script>

<template>
  <section class="admin-page-stack admin-user-detail-page">
    <RouterLink class="admin-back-link" to="/admin/users">← 返回用户列表</RouterLink>
    <PageState :loading="loading" :error="error && !user ? error : ''" :empty="!loading && !user" empty-title="没有找到该用户" @retry="load">
      <template v-if="user">
        <header class="admin-user-detail-hero"><div class="admin-user-detail-profile"><span>{{ initials }}</span><div><p class="eyebrow">USER PROFILE</p><h1>{{ user.nickname }}</h1><p>@{{ user.username }} · {{ user.user_id }}</p></div></div><div class="admin-user-detail-status"><span><i :class="user.account_status" />{{ statusLabel(user.account_status) }}</span><small>注册于 {{ dateTime(user.registered_at) }}</small></div></header>
        <p v-if="notice" class="alert success">{{ notice }}</p><p v-if="error" class="alert error">{{ error }}</p>
        <nav class="admin-detail-tabs"><button v-for="item in ([['overview','总览'],['profile','资料编辑'],['security','安全与余额'],['permissions','角色权限'],['activity','操作记录']] as const)" :key="item[0]" :class="{ active: tab === item[0] }" @click="tab = item[0]">{{ item[1] }}</button></nav>

        <div v-if="tab === 'overview'" class="admin-user-overview-grid">
          <article class="admin-panel"><header><div><p class="eyebrow">ACCOUNT</p><h2>账号概况</h2></div></header><dl class="admin-clean-dl"><dt>用户名</dt><dd>{{ user.username }}</dd><dt>昵称</dt><dd>{{ user.nickname }}</dd><dt>账号状态</dt><dd>{{ statusLabel(user.account_status) }}</dd><dt>最近登录</dt><dd>{{ dateTime(user.last_login_at) }}</dd><dt>有效角色</dt><dd>{{ activeGrantCount }} 个</dd></dl></article>
          <article class="admin-panel"><header><div><p class="eyebrow">RELATIONSHIPS</p><h2>用户业务关系</h2></div></header><div class="admin-related-links"><RouterLink :to="{ path: '/admin/orders', query: { q: user.user_id } }"><span>▤</span><div><strong>购买订单</strong><small>查看订单、支付、物流和售后</small></div><b>→</b></RouterLink><RouterLink to="/admin/support/tickets"><span>◍</span><div><strong>客服消息</strong><small>查看该用户发起的平台服务工单</small></div><b>→</b></RouterLink><button @click="tab = 'security'"><span>¥</span><div><strong>账户余额</strong><small>通过资金流水执行增加或扣减</small></div><b>→</b></button></div></article>
          <article class="admin-panel admin-user-actions-panel"><header><div><p class="eyebrow">QUICK ACTIONS</p><h2>常用安全操作</h2></div></header><label>操作原因<textarea v-model.trim="reason" maxlength="500" placeholder="冻结、恢复、下线前请填写原因" /></label><div class="actions"><button v-if="user.account_status === 'active'" class="danger" :disabled="busy" @click="changeStatus('suspend')">冻结账号</button><button v-else :disabled="busy" @click="changeStatus('resume')">恢复账号</button><button class="secondary" :disabled="busy" @click="revokeSessions">强制下线</button></div></article>
        </div>

        <div v-else-if="tab === 'profile'" class="admin-detail-two-column">
          <form class="admin-panel admin-editor" @submit.prevent="saveProfile"><header><div><p class="eyebrow">BASIC INFO</p><h2>编辑用户资料</h2></div></header><label>用户名<input v-model.trim="profile.username" required minlength="4" maxlength="32" /><small v-if="fieldErrors.username" class="error-text">{{ fieldErrors.username }}</small></label><label>昵称<input v-model.trim="profile.nickname" required maxlength="64" /></label><label>更换邮箱（不修改请留空）<input v-model.trim="profile.email" type="email" placeholder="输入新的完整邮箱" /><small v-if="fieldErrors.email" class="error-text">{{ fieldErrors.email }}</small></label><button :disabled="busy">保存资料</button></form>
          <article class="admin-panel admin-editor"><header><div><p class="eyebrow">PRIVACY</p><h2>查看当前邮箱</h2></div></header><p class="muted">邮箱属于敏感信息，需要创建与当前会话绑定的一次性凭据，读取后自动失效。</p><label>查看原因<textarea v-model.trim="reason" maxlength="500" placeholder="例如：协助用户核对找回邮箱" /></label><button v-if="!sensitiveGrant" :disabled="busy" @click="createSensitiveGrant">创建一次性查看凭据</button><button v-else class="secondary" :disabled="busy" @click="reveal">查看并立即销毁凭据</button><dl v-if="sensitive" class="admin-clean-dl"><template v-for="(value, key) in sensitive" :key="key"><dt>{{ key === 'email' ? '当前邮箱' : key }}</dt><dd>{{ value }}</dd></template></dl></article>
        </div>

        <div v-else-if="tab === 'security'" class="admin-detail-two-column">
          <form class="admin-panel admin-editor" @submit.prevent="savePassword"><header><div><p class="eyebrow">PASSWORD</p><h2>设置临时密码</h2></div></header><p class="alert warning">密码不会被展示或记录。提交后用户的所有登录会话立即失效。</p><label>临时密码<input v-model="password.temporary_password" required type="password" autocomplete="new-password" placeholder="不能为空且不能包含空白字符" /></label><label class="check-row"><input v-model="password.require_change_on_next_login" type="checkbox" />要求用户下次登录时修改密码</label><label>操作原因<textarea v-model.trim="password.reason" required maxlength="500" /></label><button :disabled="busy || !auth.has('users:force_password_reset')">确认重置密码</button><small v-if="!auth.has('users:force_password_reset')" class="muted">当前管理员没有重置用户密码的权限。</small></form>
          <form class="admin-panel admin-editor" @submit.prevent="adjustWallet"><header><div><p class="eyebrow">WALLET</p><h2>调整账户余额</h2></div></header><p class="muted">不能直接覆盖余额。每次调整都会生成不可修改的资金流水和管理员审计记录。</p><div class="field-grid"><label>调整方式<select v-model="wallet.direction"><option value="credit">增加余额</option><option value="debit">扣减余额</option></select></label><label>金额（元）<input v-model.number="wallet.amount_yuan" required type="number" min="0.01" max="1000000" step="0.01" /></label></div><label>调整原因<textarea v-model.trim="wallet.reason" required maxlength="500" /></label><p v-if="walletResult" class="alert success">调整后余额：¥{{ (Number(walletResult.balance_minor) / 100).toFixed(2) }}</p><button :disabled="busy">确认并生成资金流水</button></form>
          <article class="admin-panel admin-danger-zone"><header><div><p class="eyebrow">DANGER ZONE</p><h2>删除用户</h2></div></header><p>只有从未产生交易且不属于商家或管理员身份的用户才能物理删除；存在历史订单时系统会自动阻止。</p><button class="danger" @click="deleteOpen = true">删除这个用户</button></article>
        </div>

        <div v-else-if="tab === 'permissions'" class="admin-panel"><header><div><p class="eyebrow">RBAC</p><h2>角色与数据范围</h2></div></header><p class="alert info">普通消费者不应获得商家或平台管理角色。权限变更属于高级安全操作。</p><div class="admin-role-cards"><article v-for="grant in grants" :key="grant.grant_id"><span>♜</span><div><strong>{{ grant.role_name }}</strong><small>{{ grant.scope_type }}:{{ grant.scope_id }} · {{ grant.status }}</small></div></article><p v-if="!grants.length" class="empty-state">没有可见角色授权</p></div></div>

        <div v-else class="admin-detail-two-column"><article class="admin-panel"><header><div><p class="eyebrow">STATUS EVENTS</p><h2>账号状态记录</h2></div></header><ol class="timeline"><li v-for="item in statusEvents" :key="item.status_event_id"><strong>{{ item.to_status }}</strong><p>{{ item.reason }}</p><time>{{ dateTime(item.effective_at) }}</time></li></ol><p v-if="!statusEvents.length" class="empty-state">暂无状态变更</p></article><article class="admin-panel"><header><div><p class="eyebrow">ROLE EVENTS</p><h2>权限变更记录</h2></div></header><ol class="timeline"><li v-for="item in grantEvents" :key="item.event_id"><strong>{{ item.event_type }}</strong><p>{{ item.reason }}</p><time>{{ dateTime(item.created_at) }}</time></li></ol><p v-if="!grantEvents.length" class="empty-state">暂无权限变更</p></article></div>
      </template>
    </PageState>

    <div v-if="deleteOpen" class="admin-form-overlay" @click.self="deleteOpen = false"><form class="admin-form-dialog admin-delete-dialog" @submit.prevent="removeUser"><header><div><p class="eyebrow">DANGEROUS ACTION</p><h2>确认删除用户</h2><p>该操作不可恢复。存在任何历史交易时，后端会拒绝删除。</p></div><button type="button" @click="deleteOpen = false">×</button></header><div class="admin-form-fields"><label class="wide">删除原因<textarea v-model.trim="deleteReason" required minlength="2" maxlength="500" /></label><label class="wide">输入 DELETE_USER 确认<input v-model.trim="deleteConfirmation" required autocomplete="off" /></label></div><footer><button type="button" class="secondary" @click="deleteOpen = false">取消</button><button class="danger" :disabled="busy || deleteConfirmation !== 'DELETE_USER'">永久删除</button></footer></form></div>
  </section>
</template>
