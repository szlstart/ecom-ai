<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { apiRequest, errorMessage } from '@/api/http'
import { useUserAuthStore } from '@/stores/user-auth'
interface Profile { user_id: string; username: string; nickname: string; locale: string; timezone: string; bound_accounts: Array<{ type: string; masked: string }>; version: number }
const auth = useUserAuthStore(), profile = ref<Profile | null>(null), etag = ref(''), error = ref(''), message = ref(''), pending = ref(false)
async function load() { const result = await apiRequest<Profile>('/users/me', {}, auth.accessToken); profile.value = result.data; etag.value = result.headers.get('etag') ?? '' }
onMounted(() => load().catch((reason) => { error.value = errorMessage(reason) }))
async function save() { if (!profile.value) return; pending.value = true; error.value = ''; try { const result = await apiRequest<Profile>('/users/me', { method: 'PATCH', headers: { 'If-Match': etag.value }, body: JSON.stringify({ nickname: profile.value.nickname, locale: profile.value.locale, timezone: profile.value.timezone }) }, auth.accessToken); profile.value = result.data; etag.value = result.headers.get('etag') ?? ''; message.value = '个人资料已保存。' } catch (reason) { error.value = errorMessage(reason) } finally { pending.value = false } }
</script>
<template><section class="settings-page"><div class="page-heading"><div><p class="eyebrow">我的</p><h1>个人信息</h1></div></div><p v-if="message" class="alert success">{{ message }}</p><p v-if="error" class="alert error">{{ error }}</p>
  <form v-if="profile" class="card" @submit.prevent="save"><label>账号<input :value="profile.username" disabled /></label><label>昵称<input v-model="profile.nickname" minlength="2" maxlength="20" required /></label><label>语言<input v-model="profile.locale" /></label><label>时区<input v-model="profile.timezone" /></label><div><strong>已绑定账号</strong><ul><li v-for="item in profile.bound_accounts" :key="item.type">{{ item.type }}：{{ item.masked }}</li></ul></div><button :disabled="pending">保存修改</button></form>
</section></template>
