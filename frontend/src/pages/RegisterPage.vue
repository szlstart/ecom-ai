<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { apiRequest, createIdempotencyKey, errorMessage } from '@/api/http'
import { useUserAuthStore, type SessionBootstrap } from '@/stores/user-auth'

interface Agreement { document_type: string; document_version: string; title: string; content_url: string }
interface RegistrationConfig { config_version: string; password_policy: { min_length: number; max_length: number }; required_agreements: Agreement[] }
const config = ref<RegistrationConfig | null>(null), username = ref(''), targetType = ref<'email' | 'phone'>('email'), target = ref(''), verificationId = ref(''), code = ref(''), password = ref(''), confirmation = ref('')
const accepted = ref<string[]>([]), pending = ref(false), error = ref(''), message = ref('')
const auth = useUserAuthStore(), router = useRouter(), requestKey = createIdempotencyKey('registration')
const ready = computed(() => config.value && accepted.value.length === config.value.required_agreements.length && verificationId.value)
onMounted(async () => { try { config.value = (await apiRequest<RegistrationConfig>('/auth/registration-config')).data } catch (reason) { error.value = errorMessage(reason) } })
async function sendCode() {
  pending.value = true; error.value = ''
  try { const response = await apiRequest<{ verification_id: string }>('/auth/verification-codes', { method: 'POST', body: JSON.stringify({ purpose: 'register', target_type: targetType.value, target: target.value, locale: 'zh-CN', challenge_token: null, change_ticket_id: null }) }); verificationId.value = response.data.verification_id; message.value = '验证码请求已受理。' }
  catch (reason) { error.value = errorMessage(reason) } finally { pending.value = false }
}
async function submit() {
  if (!config.value) return
  if (password.value !== confirmation.value) { error.value = '两次输入的密码不一致。'; return }
  pending.value = true; error.value = ''
  try {
    const response = await apiRequest<SessionBootstrap>('/auth/registrations', { method: 'POST', headers: { 'Idempotency-Key': requestKey }, body: JSON.stringify({ username: username.value, target_type: targetType.value, target: target.value, verification_id: verificationId.value, verification_code: code.value, password: password.value, config_version: config.value.config_version, agreement_acceptances: config.value.required_agreements.map(({ document_type, document_version }) => ({ document_type, document_version })), locale: 'zh-CN', timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai' }) })
    auth.accept(response.data); await router.replace('/me')
  } catch (reason) { error.value = errorMessage(reason) } finally { pending.value = false }
}
</script>
<template><form aria-labelledby="register-title" @submit.prevent="submit"><p class="eyebrow">用户端</p><h1 id="register-title">注册账号</h1>
  <p v-if="message" class="alert success" role="status">{{ message }}</p><p v-if="error" class="alert error" role="alert">{{ error }}</p>
  <label>用户名<input v-model="username" autocomplete="username" minlength="4" maxlength="32" pattern="[A-Za-z0-9_]+" required /></label>
  <label>验证方式<select v-model="targetType"><option value="email">邮箱</option><option value="phone">手机号</option></select></label><label>手机号或邮箱<input v-model="target" required /></label>
  <button class="secondary" :disabled="pending || !target" type="button" @click="sendCode">发送验证码</button><label>验证码<input v-model="code" autocomplete="one-time-code" maxlength="6" required /></label>
  <label>密码<input v-model="password" autocomplete="new-password" :minlength="config?.password_policy.min_length ?? 15" required type="password" /><small>至少 {{ config?.password_policy.min_length ?? 15 }} 个字符，支持密码管理器。</small></label>
  <label>确认密码<input v-model="confirmation" autocomplete="new-password" required type="password" /></label>
  <fieldset v-if="config"><legend>协议确认</legend><label v-for="item in config.required_agreements" :key="item.document_type" class="check-row"><input v-model="accepted" :value="item.document_type" type="checkbox" /><span>我已阅读并同意 <RouterLink :to="`/legal/${item.document_type}?version=${item.document_version}`" target="_blank">{{ item.title }}</RouterLink></span></label></fieldset>
  <button :disabled="pending || !ready" type="submit">同意协议并注册</button><p class="form-help">已有账号？<RouterLink to="/login">返回登录</RouterLink></p>
</form></template>
