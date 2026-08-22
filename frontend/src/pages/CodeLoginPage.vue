<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { apiRequest, errorMessage } from '@/api/http'
import { useUserAuthStore, type SessionBootstrap } from '@/stores/user-auth'

const targetType = ref<'email' | 'phone'>('email'), target = ref(''), verificationId = ref(''), code = ref('')
const pending = ref(false), message = ref(''), error = ref('')
const router = useRouter(), auth = useUserAuthStore()
async function sendCode() {
  pending.value = true; error.value = ''
  try {
    const response = await apiRequest<{ verification_id: string }>('/auth/verification-codes', { method: 'POST', body: JSON.stringify({ purpose: 'login', target_type: targetType.value, target: target.value, locale: 'zh-CN', challenge_token: null, change_ticket_id: null }) })
    verificationId.value = response.data.verification_id; message.value = '验证码请求已受理，请查看对应渠道。'
  } catch (reason) { error.value = errorMessage(reason) } finally { pending.value = false }
}
async function submit() {
  pending.value = true; error.value = ''
  try {
    const response = await apiRequest<SessionBootstrap>('/auth/login', { method: 'POST', body: JSON.stringify({ auth_method: 'verification_code', target_type: targetType.value, target: target.value, verification_id: verificationId.value, verification_code: code.value, client: { client_type: 'web', device_name: navigator.userAgent.slice(0, 80) }, challenge_token: null }) })
    auth.accept(response.data); await router.replace('/me')
  } catch (reason) { error.value = errorMessage(reason) } finally { pending.value = false }
}
</script>
<template><form aria-labelledby="code-login-title" @submit.prevent="submit">
  <p class="eyebrow">用户端</p><h1 id="code-login-title">验证码登录</h1>
  <p v-if="message" class="alert success" role="status">{{ message }}</p><p v-if="error" class="alert error" role="alert">{{ error }}</p>
  <label>验证方式<select v-model="targetType"><option value="email">邮箱</option><option value="phone">手机号</option></select></label>
  <label>手机号或邮箱<input v-model="target" autocomplete="username" required /></label>
  <button class="secondary" :disabled="pending || !target" type="button" @click="sendCode">发送验证码</button>
  <label>验证码<input v-model="code" autocomplete="one-time-code" inputmode="numeric" maxlength="6" required /></label>
  <button :disabled="pending || !verificationId" type="submit">登录</button><p class="form-help"><RouterLink to="/login">返回密码登录</RouterLink></p>
</form></template>
