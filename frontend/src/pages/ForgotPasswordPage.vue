<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { apiRequest, errorMessage } from '@/api/http'
import { usePasswordResetStore } from '@/stores/password-reset'
const targetType = ref<'email' | 'phone'>('email'), target = ref(''), verificationId = ref(''), code = ref('')
const error = ref(''), message = ref(''), pending = ref(false), router = useRouter(), resetStore = usePasswordResetStore()
async function sendCode() {
  pending.value = true; error.value = ''
  try { const response = await apiRequest<{ verification_id: string }>('/auth/verification-codes', { method: 'POST', body: JSON.stringify({ purpose: 'reset_password', target_type: targetType.value, target: target.value, locale: 'zh-CN', challenge_token: null, change_ticket_id: null }) }); verificationId.value = response.data.verification_id; message.value = '如该账号可用，验证码将发送到对应渠道。' }
  catch (reason) { error.value = errorMessage(reason) } finally { pending.value = false }
}
async function verify() {
  pending.value = true; error.value = ''
  try { const response = await apiRequest<{ reset_ticket: string }>('/auth/password-reset-tickets', { method: 'POST', body: JSON.stringify({ target_type: targetType.value, target: target.value, verification_id: verificationId.value, verification_code: code.value }) }); resetStore.setTicket(response.data.reset_ticket); await router.push('/reset-password') }
  catch (reason) { error.value = errorMessage(reason) } finally { pending.value = false }
}
</script>
<template><form aria-labelledby="forgot-title" @submit.prevent="verify">
  <p class="eyebrow">账号安全</p><h1 id="forgot-title">找回密码</h1><p v-if="message" class="alert success" role="status">{{ message }}</p><p v-if="error" class="alert error" role="alert">{{ error }}</p>
  <label>验证方式<select v-model="targetType"><option value="email">邮箱</option><option value="phone">手机号</option></select></label><label>联系方式<input v-model="target" required /></label>
  <button class="secondary" :disabled="pending || !target" type="button" @click="sendCode">发送验证码</button><label>验证码<input v-model="code" autocomplete="one-time-code" maxlength="6" required /></label>
  <button :disabled="pending || !verificationId" type="submit">继续</button><p class="form-help"><RouterLink to="/login">返回登录</RouterLink></p>
</form></template>
