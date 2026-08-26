<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'

import { errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const router = useRouter()
const identifier = ref('')
const password = ref('')
const error = ref('')
const pending = ref(false)

async function submit() {
  pending.value = true
  error.value = ''
  try {
    await auth.passwordLogin(identifier.value, password.value, navigator.userAgent.slice(0, 80))
    password.value = ''
    await router.push('/merchant/login/mfa')
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <form class="merchant-login-form" aria-labelledby="merchant-login-title" @submit.prevent="submit">
    <div><p class="eyebrow">商家登录</p><h1 id="merchant-login-title">欢迎回来</h1><p class="muted">使用平台分配的店铺运营账号登录。</p></div>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <label>商家账号<input v-model.trim="identifier" autocomplete="username" required autofocus /></label>
    <label>密码<input v-model="password" autocomplete="current-password" type="password" required /></label>
    <button :disabled="pending">{{ pending ? '正在验证…' : '下一步：安全验证' }}</button>
    <p class="merchant-login-help">消费者请前往 <RouterLink to="/">商城首页</RouterLink>，平台人员请前往 <RouterLink to="/admin/login">平台管理端</RouterLink>。</p>
  </form>
</template>
