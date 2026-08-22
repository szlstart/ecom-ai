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
const completedUntil = ref('')
const key = createIdempotencyKey('admin-mfa')

onMounted(async () => {
  if (!auth.challenge && !auth.isAuthenticated) await auth.refresh()
})

async function submit() {
  pending.value = true
  error.value = ''
  try {
    if (auth.isAuthenticated && !auth.challenge) {
      const result = await auth.reauthenticate(password.value, method.value, code.value)
      completedUntil.value = result.reauth_expires_at
      password.value = ''
      code.value = ''
      const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : ''
      if (redirect.startsWith('/admin/') && !redirect.startsWith('//')) {
        await router.replace(redirect)
      }
      return
    }
    await auth.verifyMfa(method.value, code.value, key)
    code.value = ''
    await router.replace('/admin/dashboard')
  } catch (reason) {
    error.value = errorMessage(reason)
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <form aria-labelledby="mfa-title" @submit.prevent="submit">
    <p class="eyebrow">多因素认证</p>
    <h1 id="mfa-title">{{ auth.isAuthenticated && !auth.challenge ? '重新安全验证' : '完成管理登录' }}</h1>
    <p v-if="!auth.challenge && !auth.isAuthenticated" class="alert warning">
      验证流程不存在或已过期。<RouterLink to="/admin/login">重新登录</RouterLink>
    </p>
    <p v-if="completedUntil" class="alert success">近期认证已更新，有效至 {{ completedUntil }}。</p>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <label v-if="auth.isAuthenticated && !auth.challenge">
      管理密码
      <input v-model="password" autocomplete="current-password" type="password" required />
    </label>
    <label>
      验证方式
      <select v-model="method">
        <option value="totp">认证器动态码</option>
        <option value="recovery_code">恢复码</option>
      </select>
    </label>
    <label>
      安全验证码
      <input v-model="code" autocomplete="one-time-code" required />
    </label>
    <button :disabled="pending || (!auth.challenge && !auth.isAuthenticated)">
      {{ pending ? '验证中…' : '完成验证' }}
    </button>
  </form>
</template>
