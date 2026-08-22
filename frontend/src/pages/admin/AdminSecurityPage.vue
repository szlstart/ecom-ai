<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { apiRequest, errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'

interface Session {
  session_id: string
  device_name: string | null
  client_type: string
  last_seen_at: string
  expires_at: string
  is_current: boolean
}

const auth = useAdminAuthStore()
const sessions = ref<Session[]>([])
const error = ref('')
const message = ref('')

async function load() {
  sessions.value = (
    await apiRequest<Session[]>('/admin/auth/sessions', {}, auth.accessToken)
  ).data
}

onMounted(() => load().catch((reason) => { error.value = errorMessage(reason) }))

async function revoke(sessionId: string) {
  error.value = ''
  try {
    await apiRequest(`/admin/auth/sessions/${sessionId}`, { method: 'DELETE' }, auth.accessToken)
    message.value = '管理会话已撤销。'
    await load()
  } catch (reason) {
    error.value = errorMessage(reason)
  }
}
</script>

<template>
  <section>
    <p class="eyebrow">管理身份安全</p>
    <h1>安全会话</h1>
    <p v-if="message" class="alert success">{{ message }}</p>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <article class="card">
      <h2>近期认证</h2>
      <p class="muted">冻结用户、查看敏感字段和变更权限前，需要重新验证密码与 MFA。</p>
      <RouterLink class="button-link" to="/admin/login/mfa?redirect=/admin/security">重新安全验证</RouterLink>
    </article>
    <article class="card">
      <h2>登录设备</h2>
      <ul class="session-list">
        <li v-for="item in sessions" :key="item.session_id">
          <div>
            <strong>{{ item.device_name || '未知设备' }}</strong>
            <small>{{ item.is_current ? '当前管理会话' : `最近活动 ${item.last_seen_at}` }}</small>
          </div>
          <button v-if="!item.is_current" class="danger small" @click="revoke(item.session_id)">退出设备</button>
        </li>
      </ul>
    </article>
  </section>
</template>
