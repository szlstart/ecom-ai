<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { apiRequest, createIdempotencyKey, errorMessage } from '@/api/http'
import { usePasswordResetStore } from '@/stores/password-reset'
const password = ref(''), confirmation = ref(''), error = ref(''), pending = ref(false)
const store = usePasswordResetStore(), router = useRouter(), requestKey = createIdempotencyKey('password-reset')
async function submit() {
  if (!password.value || /\s/u.test(password.value)) { error.value = '密码不能为空，也不能包含空格、换行或其他空白字符。'; return }
  if (password.value !== confirmation.value) { error.value = '两次输入的密码不一致。'; return }
  if (!store.ticket) return; pending.value = true
  try { await apiRequest('/auth/password-resets', { method: 'POST', headers: { 'Idempotency-Key': requestKey }, body: JSON.stringify({ reset_ticket: store.ticket, new_password: password.value }) }); store.clear(); await router.replace('/login?reset=success') }
  catch (reason) { error.value = errorMessage(reason) } finally { pending.value = false }
}
</script>
<template><form aria-labelledby="reset-title" @submit.prevent="submit"><p class="eyebrow">账号安全</p><h1 id="reset-title">设置新密码</h1>
  <p v-if="!store.ticket" class="alert error">重置流程已丢失或过期，请<RouterLink to="/forgot-password">重新找回</RouterLink>。</p><p v-if="error" class="alert error" role="alert">{{ error }}</p>
  <label>新密码<input v-model="password" autocomplete="new-password" required type="password" /><small>密码不能为空，且不能包含空格、换行或其他空白字符；长度不限。</small></label><label>确认新密码<input v-model="confirmation" autocomplete="new-password" required type="password" /></label>
  <button :disabled="pending || !store.ticket" type="submit">重置密码</button></form></template>
