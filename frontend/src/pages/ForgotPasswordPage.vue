<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { ApiProblem, apiRequest, errorMessage } from '@/api/http'
import { usePasswordResetStore } from '@/stores/password-reset'
const username = ref(''), maskedEmail = ref(''), email = ref('')
const error = ref(''), emailError = ref(''), pending = ref(false), router = useRouter(), resetStore = usePasswordResetStore()
async function getHint() {
  pending.value = true; error.value = ''
  try { const response = await apiRequest<{ email_masked: string }>('/auth/password-reset-hints', { method: 'POST', body: JSON.stringify({ username: username.value }) }); maskedEmail.value = response.data.email_masked; email.value = ''; emailError.value = '' }
  catch (reason) { error.value = errorMessage(reason) } finally { pending.value = false }
}
async function verify() {
  pending.value = true; error.value = ''; emailError.value = ''
  try { const response = await apiRequest<{ reset_ticket: string }>('/auth/password-reset-tickets', { method: 'POST', body: JSON.stringify({ username: username.value, email: email.value }) }); resetStore.setTicket(response.data.reset_ticket); await router.push('/reset-password') }
  catch (reason) {
    if (reason instanceof ApiProblem) emailError.value = reason.body.errors?.find((item) => item.pointer === '/email')?.message ?? ''
    if (!emailError.value) error.value = errorMessage(reason)
  } finally { pending.value = false }
}
</script>
<template><form aria-labelledby="forgot-title" @submit.prevent="maskedEmail ? verify() : getHint()">
  <p class="eyebrow">账号安全</p><h1 id="forgot-title">找回密码</h1><p v-if="error" class="alert error" role="alert">{{ error }}</p>
  <label>用户名<input v-model.trim="username" autocomplete="username" :disabled="Boolean(maskedEmail)" minlength="4" maxlength="32" required /></label>
  <template v-if="maskedEmail">
    <p class="alert success" role="status">该账号登记邮箱：<strong>{{ maskedEmail }}</strong></p>
    <label>完整邮箱<input v-model.trim="email" autocomplete="email" type="email" required /><small>请输入注册时填写的完整邮箱，匹配成功后即可设置新密码。</small><small v-if="emailError" class="field-error" role="alert">{{ emailError }}</small></label>
    <button :disabled="pending || !email" type="submit">核对邮箱并继续</button>
    <button class="secondary" :disabled="pending" type="button" @click="maskedEmail = ''; email = ''; emailError = ''">更换用户名</button>
  </template>
  <button v-else :disabled="pending || !username" type="submit">查看邮箱提示</button>
  <p class="form-help"><RouterLink to="/login">返回登录</RouterLink></p>
</form></template>
