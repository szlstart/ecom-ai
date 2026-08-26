<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { ApiProblem, apiRequest, createIdempotencyKey, errorMessage } from '@/api/http'
import { useUserAuthStore } from '@/stores/user-auth'

interface Security {
  password_set: boolean
  password_changed_at: string | null
  current_email: string | null
  bound_accounts: Array<{ type: string; masked: string }>
  active_session_count: number
}
interface Session {
  session_id: string
  device_name: string | null
  last_seen_at: string
  expires_at: string
  is_current: boolean
}

const auth = useUserAuthStore()
const security = ref<Security | null>(null)
const sessions = ref<Session[]>([])
const error = ref('')
const emailError = ref('')
const message = ref('')
const currentPassword = ref('')
const newPassword = ref('')
const newEmail = ref('')

async function load() {
  security.value = (await apiRequest<Security>('/users/me/security', {}, auth.accessToken)).data
  sessions.value = (await apiRequest<Session[]>('/auth/sessions', {}, auth.accessToken)).data
}

onMounted(() => load().catch((reason) => { error.value = errorMessage(reason) }))

async function changePassword() {
  error.value = ''
  message.value = ''
  if (!newPassword.value || /\s/u.test(newPassword.value)) {
    error.value = '新密码不能为空，也不能包含空格、换行或其他空白字符。'
    return
  }
  try {
    await apiRequest('/users/me/password', {
      method: 'PUT',
      headers: { 'Idempotency-Key': createIdempotencyKey('password-change') },
      body: JSON.stringify({ current_password: currentPassword.value, new_password: newPassword.value }),
    }, auth.accessToken)
    currentPassword.value = ''
    newPassword.value = ''
    message.value = '密码已修改。'
    await load()
  } catch (reason) {
    error.value = errorMessage(reason)
  }
}

async function changeEmail() {
  error.value = ''
  emailError.value = ''
  message.value = ''
  try {
    await apiRequest('/users/me/contact-changes', {
      method: 'POST',
      headers: { 'Idempotency-Key': createIdempotencyKey('email-change') },
      body: JSON.stringify({ new_email: newEmail.value }),
    }, auth.accessToken)
    newEmail.value = ''
    message.value = '邮箱已更新。'
    await load()
  } catch (reason) {
    if (reason instanceof ApiProblem) {
      emailError.value = reason.body.errors?.find((item) => item.pointer === '/new_email')?.message ?? ''
    }
    if (!emailError.value) error.value = errorMessage(reason)
  }
}

async function revoke(sessionId: string) {
  error.value = ''
  try {
    await apiRequest(`/auth/sessions/${sessionId}`, { method: 'DELETE' }, auth.accessToken)
    await load()
  } catch (reason) {
    error.value = errorMessage(reason)
  }
}
</script>

<template>
  <section class="settings-page">
    <p class="eyebrow">我的</p>
    <h1>账号安全</h1>
    <p v-if="message" class="alert success" role="status">{{ message }}</p>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>

    <div class="settings-grid">
      <form class="card" @submit.prevent="changePassword">
        <h2>修改密码</h2>
        <label>当前密码<input v-model="currentPassword" autocomplete="current-password" type="password" required /></label>
        <label>新密码<input v-model="newPassword" autocomplete="new-password" type="password" required /><small>密码不能为空，且不能包含空格、换行或其他空白字符；长度不限。</small></label>
        <button>修改密码</button>
      </form>

      <form class="card" @submit.prevent="changeEmail">
        <h2>更换邮箱</h2>
        <label>当前邮箱<output class="readonly-field">{{ security?.current_email || '尚未设置' }}</output></label>
        <label>新的邮箱<input v-model.trim="newEmail" autocomplete="email" type="email" required /><small v-if="emailError" class="field-error" role="alert">{{ emailError }}</small></label>
        <button>确认换绑</button>
      </form>
    </div>

    <article class="card">
      <div class="page-heading"><h2>登录设备（{{ security?.active_session_count ?? 0 }}）</h2></div>
      <ul class="session-list">
        <li v-for="item in sessions" :key="item.session_id">
          <div><strong>{{ item.device_name || '未知设备' }}</strong><small>{{ item.is_current ? '当前会话' : `最近活动 ${item.last_seen_at}` }}</small></div>
          <button v-if="!item.is_current" class="danger small" type="button" @click="revoke(item.session_id)">退出设备</button>
        </li>
      </ul>
    </article>
  </section>
</template>
