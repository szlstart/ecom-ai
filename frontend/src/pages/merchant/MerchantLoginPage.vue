<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'

import { errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const router = useRouter()
const mode = ref<'login' | 'register'>('login')
const identifier = ref('')
const password = ref('')
const registration = ref({ username: '', password: '', confirmPassword: '', storeName: '' })
const error = ref('')
const pending = ref(false)

function switchMode(nextMode: 'login' | 'register') {
  mode.value = nextMode
  error.value = ''
}

async function submitLogin() {
  pending.value = true
  error.value = ''
  try {
    await auth.merchantPasswordLogin(identifier.value, password.value, navigator.userAgent.slice(0, 80))
    password.value = ''
    const redirect = typeof router.currentRoute.value.query.redirect === 'string'
      && router.currentRoute.value.query.redirect.startsWith('/merchant/')
      ? router.currentRoute.value.query.redirect
      : '/merchant/products'
    await router.replace(redirect)
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    pending.value = false
  }
}

async function submitRegistration() {
  error.value = ''
  if (registration.value.password !== registration.value.confirmPassword) {
    error.value = '两次输入的密码不一致。'
    return
  }
  if (/\s/.test(registration.value.password)) {
    error.value = '密码不能包含空格、换行或其他空白字符。'
    return
  }
  pending.value = true
  try {
    await auth.merchantRegister(
      registration.value.username,
      registration.value.password,
      registration.value.storeName,
      navigator.userAgent.slice(0, 80),
    )
    registration.value.password = ''
    registration.value.confirmPassword = ''
    await router.replace('/merchant/products')
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <section class="merchant-login-form" aria-labelledby="merchant-login-title">
    <div><p class="eyebrow">商家入口</p><h1 id="merchant-login-title">{{ mode === 'login' ? '欢迎回来' : '创建你的店铺' }}</h1><p class="muted">商家账号与消费者账号、平台管理员账号相互独立。</p></div>
    <div class="merchant-auth-tabs" role="tablist" aria-label="商家登录或注册"><button type="button" role="tab" :aria-selected="mode === 'login'" :class="{ active: mode === 'login' }" @click="switchMode('login')">登录</button><button type="button" role="tab" :aria-selected="mode === 'register'" :class="{ active: mode === 'register' }" @click="switchMode('register')">注册店铺</button></div>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <form v-if="mode === 'login'" class="merchant-auth-fields" @submit.prevent="submitLogin"><label>商家账号<input v-model.trim="identifier" autocomplete="username" required autofocus /></label><label>密码<input v-model="password" autocomplete="current-password" type="password" required /></label><button :disabled="pending">{{ pending ? '正在登录…' : '登录商家中心' }}</button></form>
    <form v-else class="merchant-auth-fields" @submit.prevent="submitRegistration"><label>商家用户名<input v-model.trim="registration.username" autocomplete="username" required minlength="4" maxlength="32" pattern="[A-Za-z0-9_]+" placeholder="4–32 位字母、数字或下划线" /></label><label>店铺名称<input v-model.trim="registration.storeName" required minlength="2" maxlength="128" placeholder="不能与已有店铺重名" /></label><label>密码<input v-model="registration.password" autocomplete="new-password" type="password" required placeholder="不能为空，也不能包含空白字符" /></label><label>再次输入密码<input v-model="registration.confirmPassword" autocomplete="new-password" type="password" required /></label><div class="alert info">注册成功后会直接进入新店铺。此账号只用于商家中心，不能登录消费者端或平台管理端。</div><button :disabled="pending">{{ pending ? '正在创建店铺…' : '注册并进入商家中心' }}</button></form>
    <p class="merchant-login-help">消费者请前往 <RouterLink to="/">商城首页</RouterLink>，平台人员请前往 <RouterLink to="/admin/login">平台管理端</RouterLink>。</p>
  </section>
</template>
