<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { apiRequest, errorMessage } from '@/api/http'
import { useUserAuthStore, type SessionBootstrap } from '@/stores/user-auth'

const identifier = ref('')
const password = ref('')
const pending = ref(false)
const error = ref('')
const router = useRouter()
const route = useRoute()
const auth = useUserAuthStore()

async function submit() {
  pending.value = true
  error.value = ''
  try {
    const response = await apiRequest<SessionBootstrap>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({
        auth_method: 'password', identifier: identifier.value, password: password.value,
        client: { client_type: 'web', device_name: navigator.userAgent.slice(0, 80) },
        challenge_token: null,
      }),
    })
    auth.accept(response.data)
    const target = typeof route.query.redirect === 'string' ? route.query.redirect : '/me'
    await router.replace(target.startsWith('/') && !target.startsWith('//') ? target : '/me')
  } catch (reason) { error.value = errorMessage(reason) } finally { pending.value = false }
}
</script>

<template>
  <form aria-labelledby="login-title" @submit.prevent="submit">
    <p class="eyebrow">用户端</p><h1 id="login-title">欢迎回来</h1>
    <p class="muted">使用用户名、手机号或邮箱登录。</p>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <label>账号<input v-model="identifier" autocomplete="username" required /></label>
    <label>密码<input v-model="password" autocomplete="current-password" required type="password" /></label>
    <button :disabled="pending" type="submit">{{ pending ? '正在登录…' : '登录' }}</button>
    <div class="form-links"><RouterLink to="/login/code">验证码登录</RouterLink><RouterLink to="/forgot-password">忘记密码</RouterLink></div>
    <p class="form-help">还没有账号？<RouterLink to="/register">注册账号</RouterLink></p>
  </form>
</template>
