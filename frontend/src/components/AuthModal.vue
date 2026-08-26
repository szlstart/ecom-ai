<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import { apiRequest, createIdempotencyKey, errorMessage } from '@/api/http'
import { useUserAuthStore, type SessionBootstrap } from '@/stores/user-auth'

interface Agreement {
  document_type: string
  document_version: string
  title: string
  content_url: string
}

interface RegistrationConfig {
  config_version: string
  password_policy: { non_empty: boolean; forbid_whitespace: boolean }
  captcha: { captcha_id: string; question: string; expires_in_seconds: number }
  required_agreements: Agreement[]
}

const props = defineProps<{ initialMode?: 'login' | 'register' }>()
const emit = defineEmits<{ close: []; authenticated: [] }>()
const auth = useUserAuthStore()
const mode = ref<'login' | 'register'>(props.initialMode ?? 'login')
const firstInput = ref<HTMLInputElement | null>(null)
const pending = ref(false)
const error = ref('')

const identifier = ref('')
const loginPassword = ref('')

const config = ref<RegistrationConfig | null>(null)
const username = ref('')
const email = ref('')
const captchaAnswer = ref('')
const password = ref('')
const confirmation = ref('')
const accepted = ref<string[]>([])
let requestKey = createIdempotencyKey('registration')

const registrationReady = computed(() => Boolean(
  config.value
  && email.value
  && captchaAnswer.value
  && accepted.value.length === config.value.required_agreements.length,
))

function switchMode(nextMode: 'login' | 'register') {
  mode.value = nextMode
  error.value = ''
  void nextTick(() => firstInput.value?.focus())
}

async function loadRegistrationConfig(force = false) {
  if (config.value && !force) return
  const previousConfigVersion = config.value?.config_version
  if (force) config.value = null
  try {
    const nextConfig = (await apiRequest<RegistrationConfig>('/auth/registration-config')).data
    if (previousConfigVersion && previousConfigVersion !== nextConfig.config_version) accepted.value = []
    config.value = nextConfig
    captchaAnswer.value = ''
  } catch (reason) {
    error.value = errorMessage(reason)
  }
}

async function login() {
  pending.value = true
  error.value = ''
  try {
    const response = await apiRequest<SessionBootstrap>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({
        auth_method: 'password',
        identifier: identifier.value,
        password: loginPassword.value,
        client: { client_type: 'web', device_name: navigator.userAgent.slice(0, 80) },
        challenge_token: null,
      }),
    })
    auth.accept(response.data)
    emit('authenticated')
  } catch (reason) {
    error.value = errorMessage(reason)
  } finally {
    pending.value = false
  }
}

async function register() {
  if (!config.value) return
  if (!password.value || /\s/u.test(password.value)) {
    error.value = '密码不能为空，也不能包含空格、换行或其他空白字符。'
    return
  }
  if (password.value !== confirmation.value) {
    error.value = '两次输入的密码不一致。'
    return
  }
  pending.value = true
  error.value = ''
  try {
    const response = await apiRequest<SessionBootstrap>('/auth/registrations', {
      method: 'POST',
      headers: { 'Idempotency-Key': requestKey },
      body: JSON.stringify({
        username: username.value,
        email: email.value,
        captcha_id: config.value.captcha.captcha_id,
        captcha_answer: captchaAnswer.value,
        password: password.value,
        config_version: config.value.config_version,
        agreement_acceptances: config.value.required_agreements.map(({ document_type, document_version }) => ({ document_type, document_version })),
        locale: 'zh-CN',
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai',
      }),
    })
    auth.accept(response.data)
    requestKey = createIdempotencyKey('registration')
    emit('authenticated')
  } catch (reason) {
    error.value = errorMessage(reason)
    await loadRegistrationConfig(true)
  } finally {
    pending.value = false
  }
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && !pending.value) emit('close')
}

watch(mode, (value) => {
  if (value === 'register') void loadRegistrationConfig()
})

onMounted(() => {
  document.body.classList.add('modal-open')
  document.addEventListener('keydown', onKeydown)
  if (mode.value === 'register') void loadRegistrationConfig()
  void nextTick(() => firstInput.value?.focus())
})

onBeforeUnmount(() => {
  document.body.classList.remove('modal-open')
  document.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <Teleport to="body">
    <div class="auth-modal-backdrop" @mousedown.self="!pending && emit('close')">
      <section class="auth-modal" role="dialog" aria-modal="true" aria-labelledby="auth-modal-title">
        <button class="auth-modal-close" type="button" aria-label="关闭注册登录弹窗" :disabled="pending" @click="emit('close')">×</button>
        <div class="auth-mode-tabs" role="tablist" aria-label="选择登录或注册">
          <button type="button" role="tab" :aria-selected="mode === 'login'" :class="{ active: mode === 'login' }" @click="switchMode('login')">登录</button>
          <button type="button" role="tab" :aria-selected="mode === 'register'" :class="{ active: mode === 'register' }" @click="switchMode('register')">注册</button>
        </div>

        <form v-if="mode === 'login'" aria-labelledby="auth-modal-title" @submit.prevent="login">
          <p class="eyebrow">用户端</p>
          <h1 id="auth-modal-title">欢迎回来</h1>
          <p class="muted">使用用户名、手机号或邮箱登录。</p>
          <p v-if="error" class="alert error" role="alert">{{ error }}</p>
          <label>账号<input ref="firstInput" v-model="identifier" autocomplete="username" required /></label>
          <label>密码<input v-model="loginPassword" autocomplete="current-password" required type="password" /></label>
          <button :disabled="pending" type="submit">{{ pending ? '正在登录…' : '登录' }}</button>
          <div class="form-links"><RouterLink to="/forgot-password" @click="emit('close')">忘记密码</RouterLink></div>
          <p class="form-help">还没有账号？<button class="link-button" type="button" @click="switchMode('register')">立即注册</button></p>
        </form>

        <form v-else aria-labelledby="auth-modal-title" @submit.prevent="register">
          <p class="eyebrow">用户端</p>
          <h1 id="auth-modal-title">注册账号</h1>
          <p v-if="error" class="alert error" role="alert">{{ error }}</p>
          <label>用户名<input ref="firstInput" v-model="username" autocomplete="username" minlength="4" maxlength="32" pattern="[A-Za-z0-9_]+" required /></label>
          <div class="arithmetic-captcha" aria-live="polite">
            <p><strong>验证码：{{ config?.captcha.question ?? '正在生成算术题…' }}</strong></p>
            <button class="link-button" type="button" :disabled="pending" @click="loadRegistrationConfig(true)">换一道题</button>
          </div>
          <label>计算结果<input v-model="captchaAnswer" inputmode="numeric" pattern="[0-9]+" autocomplete="off" required /></label>
          <label>密码<input v-model="password" autocomplete="new-password" required type="password" /><small>密码不能为空，且不能包含空格、换行或其他空白字符；长度不限。</small></label>
          <label>确认密码<input v-model="confirmation" autocomplete="new-password" required type="password" /></label>
          <label>邮箱<input v-model.trim="email" autocomplete="email" maxlength="254" required type="email" /><small>仅用于忘记密码时核对账号，不发送验证码，也不会用于营销。</small></label>
          <fieldset v-if="config"><legend>协议确认</legend><label v-for="item in config.required_agreements" :key="item.document_type" class="check-row"><input v-model="accepted" :value="item.document_type" type="checkbox" /><span>我已阅读并同意 <RouterLink :to="`/legal/${item.document_type}?version=${item.document_version}`" target="_blank">{{ item.title }}</RouterLink></span></label></fieldset>
          <button :disabled="pending || !registrationReady" type="submit">同意协议并注册</button>
          <p class="form-help">已有账号？<button class="link-button" type="button" @click="switchMode('login')">返回登录</button></p>
        </form>
      </section>
    </div>
  </Teleport>
</template>
