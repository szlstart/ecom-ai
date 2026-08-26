<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const route = useRoute()
const router = useRouter()
const password = ref('')
const error = ref('')
const pending = ref(false)

async function submit() {
  pending.value = true
  error.value = ''
  try {
    await auth.reauthenticatePlatformPassword(password.value)
    password.value = ''
    const redirect = typeof route.query.redirect === 'string'
      && route.query.redirect.startsWith('/admin/')
      ? route.query.redirect
      : '/admin/dashboard'
    await router.replace(redirect)
  } catch (reason) {
    error.value = errorMessage(reason)
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <form aria-labelledby="admin-reauth-title" @submit.prevent="submit">
    <p class="eyebrow">敏感操作确认</p>
    <h1 id="admin-reauth-title">确认管理员密码</h1>
    <p class="muted">登录时间较久时，执行高风险操作前只需再次输入当前密码。</p>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <label>当前密码<input v-model="password" autocomplete="current-password" type="password" required autofocus /></label>
    <button :disabled="pending">{{ pending ? '正在确认…' : '确认并继续' }}</button>
    <RouterLink to="/admin/dashboard">返回管理仪表盘</RouterLink>
  </form>
</template>
