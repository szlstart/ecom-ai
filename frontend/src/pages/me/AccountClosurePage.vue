<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { apiRequest, createIdempotencyKey, errorMessage } from '@/api/http'
import { useUserAuthStore } from '@/stores/user-auth'
const reasonCode = ref('no_longer_needed'), reason = ref(''), confirmation = ref(''), error = ref(''), pending = ref(false), auth = useUserAuthStore(), router = useRouter()
async function submit() { if (!confirm('注销申请会退出所有设备并进入冷静期，确定继续吗？')) return; pending.value = true; try { await apiRequest('/users/me/account-closure-requests', { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('closure') }, body: JSON.stringify({ reason_code: reasonCode.value, reason: reason.value || null, confirmation: confirmation.value }) }, auth.accessToken); auth.clear(); await router.replace('/login?closure=accepted') } catch (cause) { error.value = errorMessage(cause) } finally { pending.value = false } }
</script>
<template><section class="narrow-page"><p class="eyebrow danger-text">高风险操作</p><h1>账号注销</h1><div class="alert warning"><strong>提交后会立即退出所有设备。</strong><p>账号进入 30 天冷静期；未完成交易等条件仍由服务端复核。</p></div><p v-if="error" class="alert error">{{ error }}</p><form class="card" @submit.prevent="submit"><label>原因<select v-model="reasonCode"><option value="no_longer_needed">不再需要</option><option value="privacy_concern">隐私顾虑</option><option value="other">其他</option></select></label><label>补充说明<textarea v-model="reason" maxlength="500" /></label><label>输入 CLOSE_MY_ACCOUNT 以确认<input v-model="confirmation" required /></label><button class="danger" :disabled="pending || confirmation !== 'CLOSE_MY_ACCOUNT'">提交注销申请</button></form></section></template>
