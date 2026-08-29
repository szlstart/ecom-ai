<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { ApiProblem, apiRequest, createIdempotencyKey, errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'

interface RegistrationConfig { captcha: { captcha_id: string; question: string; expires_in_seconds: number } }
const auth = useAdminAuthStore(), router = useRouter()
const mode = ref<'login' | 'register' | 'recover'>('login'), identifier = ref(''), password = ref('')
const registration = ref({ username: '', email: '', password: '', confirmPassword: '', storeName: '', captchaAnswer: '' })
const config = ref<RegistrationConfig | null>(null), error = ref(''), pending = ref(false), fieldError = ref('')
const recovery = ref({ username: '', maskedEmail: '', email: '', ticket: '', password: '', confirmation: '' })

async function loadCaptcha() { try { config.value = (await apiRequest<RegistrationConfig>('/auth/registration-config')).data; registration.value.captchaAnswer = '' } catch (cause) { error.value = errorMessage(cause) } }
function switchMode(next: typeof mode.value) { mode.value = next; error.value = ''; fieldError.value = ''; if (next === 'register') void loadCaptcha() }
async function submitLogin() { pending.value = true; error.value = ''; try { await auth.merchantPasswordLogin(identifier.value, password.value, navigator.userAgent.slice(0, 80)); password.value = ''; const redirect = typeof router.currentRoute.value.query.redirect === 'string' && router.currentRoute.value.query.redirect.startsWith('/merchant/') ? router.currentRoute.value.query.redirect : '/merchant/products'; await router.replace(redirect) } catch (cause) { error.value = errorMessage(cause) } finally { pending.value = false } }
async function submitRegistration() {
  error.value = ''; fieldError.value = ''
  if (registration.value.password !== registration.value.confirmPassword) { error.value = '两次输入的密码不一致。'; return }
  if (/\s/u.test(registration.value.password)) { error.value = '密码不能包含空格、换行或其他空白字符。'; return }
  if (!config.value) { await loadCaptcha(); return }
  pending.value = true
  try { await auth.merchantRegister(registration.value.username, registration.value.email, registration.value.password, registration.value.storeName, config.value.captcha.captcha_id, registration.value.captchaAnswer, navigator.userAgent.slice(0, 80)); await router.replace('/merchant/products') }
  catch (cause) { error.value = errorMessage(cause); await loadCaptcha() } finally { pending.value = false }
}
async function recoveryHint() { pending.value = true; error.value = ''; try { recovery.value.maskedEmail = (await apiRequest<{ email_masked: string }>('/auth/password-reset-hints', { method: 'POST', body: JSON.stringify({ username: recovery.value.username, audience: 'merchant' }) })).data.email_masked } catch (cause) { error.value = errorMessage(cause) } finally { pending.value = false } }
async function recoveryVerify() { pending.value = true; fieldError.value = ''; error.value = ''; try { recovery.value.ticket = (await apiRequest<{ reset_ticket: string }>('/auth/password-reset-tickets', { method: 'POST', body: JSON.stringify({ username: recovery.value.username, email: recovery.value.email, audience: 'merchant' }) })).data.reset_ticket } catch (cause) { if (cause instanceof ApiProblem) fieldError.value = cause.body.errors?.find((item) => item.pointer === '/email')?.message ?? ''; if (!fieldError.value) error.value = errorMessage(cause) } finally { pending.value = false } }
async function recoveryReset() { if (!recovery.value.password || /\s/u.test(recovery.value.password)) { error.value = '新密码不能为空且不能包含空白字符。'; return }; if (recovery.value.password !== recovery.value.confirmation) { error.value = '两次输入的新密码不一致。'; return }; pending.value = true; error.value = ''; try { await apiRequest('/auth/password-resets', { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('merchant-password-reset') }, body: JSON.stringify({ reset_ticket: recovery.value.ticket, new_password: recovery.value.password }) }); identifier.value = recovery.value.username; password.value = ''; switchMode('login'); error.value = '密码已重置，请使用新密码登录。' } catch (cause) { error.value = errorMessage(cause) } finally { pending.value = false } }
onMounted(loadCaptcha)
</script>

<template>
  <section class="merchant-login-form" aria-labelledby="merchant-login-title">
    <div><p class="eyebrow">商家入口</p><h1 id="merchant-login-title">{{ mode === 'login' ? '欢迎回来' : mode === 'register' ? '创建你的店铺' : '找回商家密码' }}</h1><p class="muted">商家账号与消费者账号、平台管理员账号相互独立。</p></div>
    <div v-if="mode !== 'recover'" class="merchant-auth-tabs"><button :class="{ active: mode === 'login' }" type="button" @click="switchMode('login')">登录</button><button :class="{ active: mode === 'register' }" type="button" @click="switchMode('register')">注册店铺</button></div>
    <p v-if="error" :class="['alert', error.includes('已重置') ? 'success' : 'error']" role="alert">{{ error }}</p>
    <form v-if="mode === 'login'" class="merchant-auth-fields" @submit.prevent="submitLogin"><label>商家账号<input v-model.trim="identifier" autocomplete="username" required autofocus /></label><label>密码<input v-model="password" autocomplete="current-password" type="password" required /></label><button :disabled="pending">{{ pending ? '正在登录…' : '登录商家中心' }}</button><button class="merchant-auth-text-button" type="button" @click="switchMode('recover')">忘记密码？</button></form>
    <form v-else-if="mode === 'register'" class="merchant-auth-fields" @submit.prevent="submitRegistration"><label>商家用户名<input v-model.trim="registration.username" required minlength="4" maxlength="32" pattern="[A-Za-z0-9_]+" placeholder="4–32 位字母、数字或下划线" /></label><label>店铺名称<input v-model.trim="registration.storeName" required minlength="2" maxlength="128" placeholder="不能与已有店铺重名" /></label><label>邮箱<input v-model.trim="registration.email" type="email" required placeholder="仅用于忘记密码和账户安全" /></label><label>密码<input v-model="registration.password" type="password" required placeholder="不能为空，也不能包含空白字符" /></label><label>再次输入密码<input v-model="registration.confirmPassword" type="password" required /></label><div class="arithmetic-captcha"><p><strong>验证码：{{ config?.captcha.question ?? '正在生成算术题…' }}</strong></p><button class="merchant-auth-text-button" type="button" @click="loadCaptcha">换一道题</button></div><label>计算结果<input v-model.trim="registration.captchaAnswer" inputmode="numeric" pattern="[0-9]+" required /></label><div class="alert info">注册成功后直接进入新店铺。此账号不能登录消费者端或平台管理端。</div><button :disabled="pending || !config">{{ pending ? '正在创建店铺…' : '注册并进入商家中心' }}</button></form>
    <div v-else class="merchant-auth-fields"><form v-if="!recovery.maskedEmail" @submit.prevent="recoveryHint"><label>商家用户名<input v-model.trim="recovery.username" required minlength="4" /></label><button :disabled="pending">查看邮箱提示</button></form><form v-else-if="!recovery.ticket" @submit.prevent="recoveryVerify"><p class="alert success">该账号登记邮箱：<strong>{{ recovery.maskedEmail }}</strong></p><label>完整邮箱<input v-model.trim="recovery.email" type="email" required /><small v-if="fieldError" class="error-text">{{ fieldError }}</small></label><button :disabled="pending">核对邮箱并继续</button></form><form v-else @submit.prevent="recoveryReset"><label>新密码<input v-model="recovery.password" type="password" required /></label><label>确认新密码<input v-model="recovery.confirmation" type="password" required /></label><button :disabled="pending">重置密码</button></form><button class="merchant-auth-text-button" type="button" @click="switchMode('login')">← 返回登录</button></div>
    <p class="merchant-login-help">消费者请前往 <RouterLink to="/">商城首页</RouterLink>，平台人员请前往 <RouterLink to="/admin/login">平台管理端</RouterLink>。</p>
  </section>
</template>
