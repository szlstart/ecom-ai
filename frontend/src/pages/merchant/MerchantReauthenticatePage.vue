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
    await auth.reauthenticateMerchant(password.value)
    password.value = ''
    const redirect = typeof route.query.redirect === 'string'
      && route.query.redirect.startsWith('/merchant/')
      ? route.query.redirect
      : '/merchant/dashboard'
    await router.replace(redirect)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <form class="merchant-login-form" aria-labelledby="merchant-reauth-title" @submit.prevent="submit">
    <div>
      <p class="eyebrow">敏感操作确认</p>
      <h1 id="merchant-reauth-title">确认当前密码</h1>
      <p class="muted">登录时间较久时，修改库存或发布商品前只需再次输入密码。</p>
    </div>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <label>当前密码<input v-model="password" type="password" autocomplete="current-password" required autofocus /></label>
    <button :disabled="pending">{{ pending ? '正在确认…' : '确认并继续' }}</button>
    <RouterLink to="/merchant/dashboard">返回商家工作台</RouterLink>
  </form>
</template>
