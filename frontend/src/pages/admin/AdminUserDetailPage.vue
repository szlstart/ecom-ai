<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { apiRequest, createIdempotencyKey, errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'

interface User {
  user_id: string
  username: string
  nickname: string
  account_status: string
  registered_at: string
  permission_version: number
  version: number
}
interface Grant {
  grant_id: string
  role_id: string
  role_name: string
  scope_type: string
  scope_id: number
  status: string
  version: number
}
interface Role {
  role_id: string
  role_name: string
  scope_type: 'platform' | 'store'
  status: string
}
interface TimelineEvent {
  event_id?: string
  status_event_id?: string
  event_type?: string
  to_status?: string
  reason: string
  created_at?: string
  effective_at?: string
}

const route = useRoute()
const auth = useAdminAuthStore()
const userId = String(route.params.userId)
const user = ref<User | null>(null)
const grants = ref<Grant[]>([])
const roles = ref<Role[]>([])
const statusEvents = ref<TimelineEvent[]>([])
const grantEvents = ref<TimelineEvent[]>([])
const etag = ref('')
const error = ref('')
const message = ref('')
const reason = ref('')
const selectedRoleId = ref('')
const grantScopeId = ref(0)
const sensitiveGrant = ref('')
const sensitiveGrantEtag = ref('')
const sensitive = ref<Record<string, string> | null>(null)

async function load() {
  const userResult = await apiRequest<User>(`/admin/users/${userId}`, {}, auth.accessToken)
  user.value = userResult.data
  etag.value = userResult.headers.get('etag') ?? ''
  grants.value = (
    await apiRequest<Grant[]>(`/admin/users/${userId}/role-grants`, {}, auth.accessToken)
  ).data
  statusEvents.value = (
    await apiRequest<TimelineEvent[]>(`/admin/users/${userId}/status-events`, {}, auth.accessToken)
  ).data
  grantEvents.value = (
    await apiRequest<TimelineEvent[]>(`/admin/users/${userId}/role-grant-events`, {}, auth.accessToken)
  ).data
  if (auth.has('rbac:read')) {
    roles.value = (await apiRequest<Role[]>('/admin/roles', {}, auth.accessToken)).data
  }
}

onMounted(() => load().catch((cause) => { error.value = errorMessage(cause) }))

async function changeStatus(action: 'suspend' | 'resume') {
  if (!reason.value) { error.value = '请填写操作原因。'; return }
  try {
    await apiRequest<User>(`/admin/users/${userId}/status-changes`, {
      method: 'POST',
      headers: { 'If-Match': etag.value, 'Idempotency-Key': createIdempotencyKey('user-status') },
      body: JSON.stringify({ action, reason_code: 'admin_decision', reason: reason.value, expires_at: null }),
    }, auth.accessToken)
    message.value = '用户状态已更新。'
    await load()
  } catch (cause) { error.value = errorMessage(cause) }
}

async function revokeSessions() {
  try {
    await apiRequest(`/admin/users/${userId}/session-revocations`, {
      method: 'POST',
      headers: { 'Idempotency-Key': createIdempotencyKey('session-revoke') },
      body: JSON.stringify({ scope: 'all', reason: reason.value || 'Security administration' }),
    }, auth.accessToken)
    message.value = '全部会话已撤销。'
  } catch (cause) { error.value = errorMessage(cause) }
}

async function requirePasswordReset() {
  try {
    await apiRequest(`/admin/users/${userId}/password-reset-requirements`, {
      method: 'POST',
      headers: { 'Idempotency-Key': createIdempotencyKey('password-reset-requirement') },
      body: JSON.stringify({ reason: reason.value || 'Security administration' }),
    }, auth.accessToken)
    message.value = '该用户下次登录前必须重置密码。'
  } catch (cause) { error.value = errorMessage(cause) }
}

async function createSensitiveGrant() {
  try {
    const result = await apiRequest<{ grant_id: string }>(
      `/admin/users/${userId}/sensitive-field-access-grants`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': createIdempotencyKey('sensitive') },
        body: JSON.stringify({ fields: ['email', 'phone'], purpose_code: 'user_support', reason: reason.value || 'Authorized support investigation', ttl_seconds: 300 }),
      },
      auth.accessToken,
    )
    sensitiveGrant.value = result.data.grant_id
    sensitiveGrantEtag.value = result.headers.get('etag') ?? ''
    message.value = '一次性访问凭据已创建。'
  } catch (cause) { error.value = errorMessage(cause) }
}

async function revokeSensitiveGrant() {
  try {
    await apiRequest(`/admin/sensitive-field-access-grants/${sensitiveGrant.value}/revocations`, {
      method: 'POST',
      headers: { 'If-Match': sensitiveGrantEtag.value, 'Idempotency-Key': createIdempotencyKey('sensitive-revoke') },
      body: JSON.stringify({ reason: reason.value || 'Access no longer required' }),
    }, auth.accessToken)
    sensitiveGrant.value = ''
    sensitiveGrantEtag.value = ''
    message.value = '临时访问凭据已撤销。'
  } catch (cause) { error.value = errorMessage(cause) }
}

async function reveal() {
  try {
    sensitive.value = (
      await apiRequest<{ values: Record<string, string> }>(
        `/admin/users/${userId}/sensitive-fields`,
        { headers: { 'X-Sensitive-Access-Grant': sensitiveGrant.value } },
        auth.accessToken,
      )
    ).data.values
    sensitiveGrant.value = ''
    sensitiveGrantEtag.value = ''
  } catch (cause) { error.value = errorMessage(cause) }
}

async function grantRole() {
  const role = roles.value.find((item) => item.role_id === selectedRoleId.value)
  if (!role || !reason.value) { error.value = '请选择角色并填写授权原因。'; return }
  try {
    await apiRequest(`/admin/users/${userId}/role-grants`, {
      method: 'POST',
      headers: { 'If-Match': etag.value, 'Idempotency-Key': createIdempotencyKey('role-grant') },
      body: JSON.stringify({ role_id: role.role_id, scope_type: role.scope_type, scope_id: role.scope_type === 'platform' ? 0 : grantScopeId.value, expires_at: null, reason: reason.value }),
    }, auth.accessToken)
    selectedRoleId.value = ''
    message.value = '角色授权已创建。'
    await load()
  } catch (cause) { error.value = errorMessage(cause) }
}

async function revokeRole(grant: Grant) {
  if (!reason.value) { error.value = '请填写撤销原因。'; return }
  try {
    await apiRequest(`/admin/users/${userId}/role-grants/${grant.grant_id}/revocations`, {
      method: 'POST',
      headers: { 'If-Match': `"v${grant.version}"`, 'Idempotency-Key': createIdempotencyKey('role-revoke') },
      body: JSON.stringify({ reason: reason.value }),
    }, auth.accessToken)
    message.value = '角色授权已撤销。'
    await load()
  } catch (cause) { error.value = errorMessage(cause) }
}
</script>

<template>
  <section v-if="user">
    <p class="eyebrow">用户治理 · {{ user.user_id }}</p>
    <h1>{{ user.nickname }}</h1>
    <p v-if="message" class="alert success">{{ message }}</p>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <div class="settings-grid">
      <article class="card">
        <h2>账号摘要</h2>
        <dl><dt>账号</dt><dd>{{ user.username }}</dd><dt>状态</dt><dd>{{ user.account_status }}</dd><dt>权限版本</dt><dd>{{ user.permission_version }}</dd></dl>
        <label>管理原因<textarea v-model="reason" /></label>
        <div class="actions">
          <button v-if="user.account_status === 'active'" class="danger" @click="changeStatus('suspend')">冻结账号</button>
          <button v-else @click="changeStatus('resume')">恢复账号</button>
          <button class="secondary" @click="revokeSessions">强制下线</button>
          <button class="secondary" @click="requirePasswordReset">要求重置密码</button>
        </div>
      </article>
      <article class="card">
        <h2>敏感字段临时访问</h2>
        <p class="muted">凭据绑定当前 Session、5 分钟有效且只能消费一次。</p>
        <button v-if="!sensitiveGrant" @click="createSensitiveGrant">创建访问凭据</button>
        <div v-else class="actions"><button class="danger" @click="reveal">一次性查看</button><button class="secondary" @click="revokeSensitiveGrant">撤销凭据</button></div>
        <dl v-if="sensitive"><template v-for="(value, key) in sensitive" :key="key"><dt>{{ key }}</dt><dd>{{ value }}</dd></template></dl>
      </article>
    </div>
    <form v-if="auth.has('rbac:manage')" class="card" @submit.prevent="grantRole">
      <h2>授予角色</h2>
      <div class="form-grid"><label>角色<select v-model="selectedRoleId" required><option value="">请选择</option><option v-for="role in roles.filter((item) => item.status === 'active')" :key="role.role_id" :value="role.role_id">{{ role.role_name }}（{{ role.scope_type }}）</option></select></label><label>店铺 Scope ID<input v-model.number="grantScopeId" min="0" type="number" /></label></div>
      <button>创建授权</button>
    </form>
    <article class="card"><h2>角色授权</h2><ul class="session-list"><li v-for="grant in grants" :key="grant.grant_id"><div><strong>{{ grant.role_name }}</strong><small>{{ grant.scope_type }}:{{ grant.scope_id }} · {{ grant.status }}</small></div><button v-if="grant.status === 'active' && auth.has('rbac:manage')" class="danger small" @click="revokeRole(grant)">撤销</button></li></ul></article>
    <div class="settings-grid"><article class="card"><h2>账号状态时间线</h2><ul><li v-for="item in statusEvents" :key="item.status_event_id">{{ item.to_status }} · {{ item.reason }} · {{ item.effective_at }}</li></ul></article><article class="card"><h2>角色事件时间线</h2><ul><li v-for="item in grantEvents" :key="item.event_id">{{ item.event_type }} · {{ item.reason }} · {{ item.created_at }}</li></ul></article></div>
  </section>
</template>
