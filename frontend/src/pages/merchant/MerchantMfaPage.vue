<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { createIdempotencyKey, errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const route = useRoute()
const router = useRouter()
const method = ref<'totp' | 'recovery_code'>('totp')
const password = ref('')
const code = ref('')
const error = ref('')
const pending = ref(false)
const key = createIdempotencyKey('merchant-mfa')

onMounted(async () => {
  if (!auth.challenge && !auth.isAuthenticated) await auth.refresh()
})

async function submit() {
  pending.value = true
  error.value = ''
  try {
    if (auth.isAuthenticated && !auth.challenge) {
      await auth.reauthenticate(password.value, method.value, code.value)
      const redirect = typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/merchant/') ? route.query.redirect : '/merchant/dashboard'
      await router.replace(redirect)
      return
    }
    await auth.verifyMfa(method.value, code.value, key)
    if (!auth.scopes.some((scope) => scope.scope_type === 'store')) {
      await auth.logout()
      error.value = '该账号没有绑定店铺，请使用店铺运营账号登录。'
      return
    }
    await router.replace('/merchant/dashboard')
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <form class="merchant-login-form" aria-labelledby="merchant-mfa-title" @submit.prevent="submit">
    <div><p class="eyebrow">账号保护</p><h1 id="merchant-mfa-title">{{ auth.isAuthenticated && !auth.challenge ? '重新验证身份' : '完成安全验证' }}</h1><p class="muted">防止他人修改你的商品、库存和客服消息。</p></div>
    <p v-if="!auth.challenge && !auth.isAuthenticated" class="alert warning">登录验证已失效，请 <RouterLink to="/merchant/login">重新登录</RouterLink>。</p>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <label v-if="auth.isAuthenticated && !auth.challenge">当前密码<input v-model="password" type="password" autocomplete="current-password" required /></label>
    <label>验证方式<select v-model="method"><option value="totp">认证器动态码</option><option value="recovery_code">恢复码</option></select></label>
    <label>安全验证码<input v-model.trim="code" autocomplete="one-time-code" required /></label>
    <button :disabled="pending || (!auth.challenge && !auth.isAuthenticated)">{{ pending ? '验证中…' : auth.isAuthenticated && !auth.challenge ? '完成重新验证' : '进入商家中心' }}</button>
    <RouterLink to="/merchant/login">返回账号密码登录</RouterLink>
  </form>
</template>
