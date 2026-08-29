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
const controlReason = ref('')
const sensitiveReason = ref('')
const profile = reactive({ username: '', nickname: '', email: '' })
const password = reactive({ temporary_password: '', require_change_on_next_login: true, reason: '' })
const wallet = reactive<{ direction: 'credit' | 'debit'; amount_yuan: number | null; reason: string }>({ direction: 'credit', amount_yuan: null, reason: '' })
const walletResult = ref<{ balance_minor: string; currency: string } | null>(null)
const deleteOpen = ref(false)
const deleteReason = ref('')
const deleteConfirmation = ref('')
const activityOpen = ref(false)
const fieldErrors = ref<Record<string, string>>({})
const sensitiveGrant = ref('')
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
  if (!user.value || controlReason.value.trim().length < 2) { error.value = '请先填写至少 2 个字的操作原因。'; return }
  await run(() => apiRequest(`/admin/users/${userId}/status-changes`, { method: 'POST', headers: { 'If-Match': etag.value, 'Idempotency-Key': createIdempotencyKey('user-status') }, body: JSON.stringify({ action, reason_code: 'ADMIN_DECISION', reason: controlReason.value.trim(), expires_at: null }) }, auth.accessToken), action === 'suspend' ? '用户已冻结并退出所有设备。' : '用户账号已恢复。')
}

async function revokeSessions() {
  await run(() => apiRequest(`/admin/users/${userId}/session-revocations`, { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('session-revoke') }, body: JSON.stringify({ scope: 'all', reason: controlReason.value.trim() || '管理员执行安全下线' }) }, auth.accessToken), '用户已从所有设备强制下线。', false)
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
  await run(async () => { const result = await apiRequest<{ grant_id: string }>(`/admin/users/${userId}/sensitive-field-access-grants`, { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('sensitive') }, body: JSON.stringify({ fields: ['email'], purpose_code: 'user_support', reason: sensitiveReason.value.trim() || '核对用户账号资料', ttl_seconds: 300 }) }, auth.accessToken); sensitiveGrant.value = result.data.grant_id }, '已创建 5 分钟有效的一次性查看凭据。', false)
}
async function reveal() {
  await run(async () => { sensitive.value = (await apiRequest<{ values: Record<string, string> }>(`/admin/users/${userId}/sensitive-fields`, { headers: { 'X-Sensitive-Access-Grant': sensitiveGrant.value } }, auth.accessToken)).data.values; sensitiveGrant.value = '' }, '敏感信息已读取，本次凭据已自动失效。', false)
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
        <header class="admin-user-detail-hero">
          <div class="admin-user-detail-profile">
            <span>{{ initials }}</span>
            <div><p class="eyebrow">用户工作台</p><h1>{{ user.nickname }}</h1><p>@{{ user.username }} · {{ user.user_id }}</p></div>
          </div>
          <div class="admin-user-detail-status"><span><i :class="user.account_status" />{{ statusLabel(user.account_status) }}</span><small>注册于 {{ dateTime(user.registered_at) }}</small></div>
        </header>
        <p v-if="notice" class="alert success">{{ notice }}</p>
        <p v-if="error" class="alert error">{{ error }}</p>

        <section class="admin-user-fact-strip" aria-label="用户概况">
          <article><small>最近登录</small><strong>{{ dateTime(user.last_login_at) }}</strong></article>
          <article><small>账号状态</small><strong>{{ statusLabel(user.account_status) }}</strong></article>
          <article><small>有效角色</small><strong>{{ activeGrantCount }} 个</strong></article>
          <article><small>用户编号</small><strong>{{ user.user_id }}</strong></article>
        </section>

        <nav class="admin-user-context-links" aria-label="用户关联业务">
          <RouterLink :to="{ path: '/admin/orders', query: { q: user.user_id } }"><span>▤</span><div><strong>购买订单</strong><small>订单、支付、物流和售后</small></div><b>→</b></RouterLink>
          <RouterLink to="/admin/support/tickets"><span>◍</span><div><strong>客服消息</strong><small>查看用户的平台服务记录</small></div><b>→</b></RouterLink>
          <button type="button" @click="activityOpen = true"><span>↺</span><div><strong>操作记录</strong><small>状态与权限变更时间线</small></div><b>弹窗查看</b></button>
        </nav>

        <section class="admin-user-workspace-section">
          <header class="admin-user-section-heading"><div><p class="eyebrow">01 · 账号资料</p><h2>资料直接编辑</h2><p>页面中直接修改常用资料；敏感邮箱按需授权查看，不重复展示账号摘要。</p></div></header>
          <div class="admin-detail-two-column">
            <form class="admin-panel admin-editor" @submit.prevent="saveProfile">
              <header><div><p class="eyebrow">基本资料</p><h2>编辑用户资料</h2></div></header>
              <label>用户名<input v-model.trim="profile.username" required minlength="4" maxlength="32" /><small v-if="fieldErrors.username" class="error-text">{{ fieldErrors.username }}</small></label>
              <label>昵称<input v-model.trim="profile.nickname" required maxlength="64" /></label>
              <label>更换邮箱（不修改请留空）<input v-model.trim="profile.email" type="email" placeholder="输入新的完整邮箱" /><small v-if="fieldErrors.email" class="error-text">{{ fieldErrors.email }}</small></label>
              <button :disabled="busy">保存资料</button>
            </form>
            <article class="admin-panel admin-editor">
              <header><div><p class="eyebrow">隐私信息</p><h2>查看当前邮箱</h2></div></header>
              <p class="muted">邮箱属于敏感信息。一次性查看凭据与当前会话绑定，读取后立即失效。</p>
              <label>查看原因<textarea v-model.trim="sensitiveReason" maxlength="500" placeholder="例如：协助用户核对找回邮箱" /></label>
              <button v-if="!sensitiveGrant" type="button" :disabled="busy" @click="createSensitiveGrant">创建一次性查看凭据</button>
              <button v-else type="button" class="secondary" :disabled="busy" @click="reveal">查看并立即销毁凭据</button>
              <dl v-if="sensitive" class="admin-clean-dl"><template v-for="(value, key) in sensitive" :key="key"><dt>{{ key === 'email' ? '当前邮箱' : key }}</dt><dd>{{ value }}</dd></template></dl>
            </article>
          </div>
        </section>

        <section class="admin-user-workspace-section">
          <header class="admin-user-section-heading"><div><p class="eyebrow">02 · 安全控制</p><h2>账号状态与登录安全</h2><p>冻结、恢复、强制下线和密码重置都保留独立审计原因。</p></div></header>
          <div class="admin-detail-two-column">
            <article class="admin-panel admin-editor">
              <header><div><p class="eyebrow">账号控制</p><h2>冻结、恢复与下线</h2></div></header>
              <label>操作原因<textarea v-model.trim="controlReason" maxlength="500" placeholder="执行冻结、恢复或下线前填写原因" /></label>
              <div class="actions"><button v-if="user.account_status === 'active'" type="button" class="danger" :disabled="busy" @click="changeStatus('suspend')">冻结账号</button><button v-else type="button" :disabled="busy" @click="changeStatus('resume')">恢复账号</button><button type="button" class="secondary" :disabled="busy" @click="revokeSessions">强制下线</button></div>
            </article>
            <form class="admin-panel admin-editor" @submit.prevent="savePassword">
              <header><div><p class="eyebrow">密码安全</p><h2>设置临时密码</h2></div></header>
              <p class="alert warning">密码不会被展示或记录。提交后用户的所有登录会话立即失效。</p>
              <label>临时密码<input v-model="password.temporary_password" required type="password" autocomplete="new-password" placeholder="不能为空且不能包含空白字符" /></label>
              <label class="check-row"><input v-model="password.require_change_on_next_login" type="checkbox" />要求用户下次登录时修改密码</label>
              <label>操作原因<textarea v-model.trim="password.reason" required maxlength="500" /></label>
              <button :disabled="busy || !auth.has('users:force_password_reset')">确认重置密码</button>
              <small v-if="!auth.has('users:force_password_reset')" class="muted">当前管理员没有重置用户密码的权限。</small>
            </form>
          </div>
        </section>

        <section class="admin-user-workspace-section">
          <header class="admin-user-section-heading"><div><p class="eyebrow">03 · 资金与权限</p><h2>余额和角色范围</h2><p>余额仅通过不可修改的资金流水调整；角色信息在同一页完整展示。</p></div></header>
          <div class="admin-detail-two-column">
            <form class="admin-panel admin-editor" @submit.prevent="adjustWallet">
              <header><div><p class="eyebrow">账户资金</p><h2>调整账户余额</h2></div></header>
              <p class="muted">不能直接覆盖余额。每次调整都会生成资金流水和管理员审计记录。</p>
              <div class="field-grid"><label>调整方式<select v-model="wallet.direction"><option value="credit">增加余额</option><option value="debit">扣减余额</option></select></label><label>金额（元）<input v-model.number="wallet.amount_yuan" required type="number" min="0.01" max="1000000" step="0.01" /></label></div>
              <label>调整原因<textarea v-model.trim="wallet.reason" required maxlength="500" /></label>
              <p v-if="walletResult" class="alert success">调整后余额：¥{{ (Number(walletResult.balance_minor) / 100).toFixed(2) }}</p>
              <button :disabled="busy">确认并生成资金流水</button>
            </form>
            <article class="admin-panel admin-editor">
              <header><div><p class="eyebrow">角色权限</p><h2>角色与数据范围</h2></div></header>
              <p class="alert info">普通消费者不应获得商家或平台管理角色。权限变更属于高级安全操作。</p>
              <div class="admin-role-cards"><article v-for="grant in grants" :key="grant.grant_id"><span>♜</span><div><strong>{{ grant.role_name }}</strong><small>{{ grant.scope_type }}:{{ grant.scope_id }} · {{ grant.status }}</small></div></article><p v-if="!grants.length" class="empty-state">没有可见角色授权</p></div>
            </article>
          </div>
        </section>

        <article class="admin-panel admin-danger-zone">
          <header><div><p class="eyebrow">04 · 危险操作</p><h2>删除用户</h2></div></header>
          <p>只有从未产生交易且不属于商家或管理员身份的用户才能物理删除；存在历史订单时系统会自动阻止。</p>
          <button type="button" class="danger" @click="deleteOpen = true">删除这个用户</button>
        </article>
      </template>
    </PageState>

    <div v-if="activityOpen" class="admin-form-overlay" @click.self="activityOpen = false">
      <section class="admin-form-dialog admin-activity-dialog" role="dialog" aria-modal="true" aria-labelledby="activity-dialog-title">
        <header><div><p class="eyebrow">操作记录</p><h2 id="activity-dialog-title">用户状态与权限时间线</h2><p>较长的审计信息集中在弹窗中，不打断日常资料编辑。</p></div><button type="button" aria-label="关闭" @click="activityOpen = false">×</button></header>
        <div class="admin-activity-dialog-body">
          <article><h3>账号状态记录</h3><ol class="timeline"><li v-for="item in statusEvents" :key="item.status_event_id"><strong>{{ item.to_status }}</strong><p>{{ item.reason }}</p><time>{{ dateTime(item.effective_at) }}</time></li></ol><p v-if="!statusEvents.length" class="empty-state">暂无状态变更</p></article>
          <article><h3>权限变更记录</h3><ol class="timeline"><li v-for="item in grantEvents" :key="item.event_id"><strong>{{ item.event_type }}</strong><p>{{ item.reason }}</p><time>{{ dateTime(item.created_at) }}</time></li></ol><p v-if="!grantEvents.length" class="empty-state">暂无权限变更</p></article>
        </div>
        <footer><button type="button" class="secondary" @click="activityOpen = false">关闭</button></footer>
      </section>
    </div>
    <div v-if="deleteOpen" class="admin-form-overlay" @click.self="deleteOpen = false"><form class="admin-form-dialog admin-delete-dialog" @submit.prevent="removeUser"><header><div><p class="eyebrow">DANGEROUS ACTION</p><h2>确认删除用户</h2><p>该操作不可恢复。存在任何历史交易时，后端会拒绝删除。</p></div><button type="button" @click="deleteOpen = false">×</button></header><div class="admin-form-fields"><label class="wide">删除原因<textarea v-model.trim="deleteReason" required minlength="2" maxlength="500" /></label><label class="wide">输入 DELETE_USER 确认<input v-model.trim="deleteConfirmation" required autocomplete="off" /></label></div><footer><button type="button" class="secondary" @click="deleteOpen = false">取消</button><button class="danger" :disabled="busy || deleteConfirmation !== 'DELETE_USER'">永久删除</button></footer></form></div>
  </section>
</template>
