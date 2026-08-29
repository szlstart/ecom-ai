<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
const identifier = ref(''), password = ref(''), error = ref(''), pending = ref(false), auth = useAdminAuthStore(), router = useRouter()
async function submit() { pending.value = true; error.value = ''; try { await auth.platformPasswordLogin(identifier.value, password.value, navigator.userAgent.slice(0, 80)); password.value = ''; await router.replace('/admin/dashboard') } catch (reason) { error.value = errorMessage(reason) } finally { pending.value = false } }
</script>
<template><form aria-labelledby="admin-login-title" @submit.prevent="submit"><p class="eyebrow">平台管理端</p><h1 id="admin-login-title">管理员登录</h1><p class="muted">这里只允许平台管理员登录，店铺账号请使用商家中心。</p><p v-if="error" class="alert error" role="alert">{{ error }}</p><label>管理员账号<input v-model="identifier" autocomplete="username" required autofocus /></label><label>密码<input v-model="password" autocomplete="current-password" type="password" required /></label><button :disabled="pending">{{ pending ? '正在登录…' : '登录管理端' }}</button></form></template>
