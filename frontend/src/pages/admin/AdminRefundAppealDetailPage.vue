<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { claimAdminAppeal, decideAdminAppeal, getAdminAppeal, isApprovalRequired } from '@/api/admin-after-sales'
import type { RefundAppeal } from '@/api/after-sales'
import { errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
const route = useRoute(), router = useRouter(), auth = useAdminAuthStore(), item = ref<RefundAppeal | null>(null), etag = ref(''), reason = ref(''), error = ref(''), busy = ref(false)
async function load() { const response = await getAdminAppeal(String(route.params.appealId), auth.accessToken!); item.value = response.data; etag.value = response.headers.get('etag') ?? '' }
onMounted(() => load().catch((cause) => { error.value = errorMessage(cause) }))
async function claim() { busy.value = true; try { const response = await claimAdminAppeal(item.value!.appeal_id, etag.value, auth.accessToken!); item.value = response.data; etag.value = response.headers.get('etag') ?? '' } catch (cause) { error.value = errorMessage(cause) } finally { busy.value = false } }
async function decide(decision: 'approve' | 'reject') { if (!reason.value.trim()) return; busy.value = true; try { const response = await decideAdminAppeal(item.value!.appeal_id, etag.value, decision, reason.value.trim(), auth.accessToken!); if (isApprovalRequired(response.data)) { await router.push(`/admin/approval-requests/${response.data.approval_request_id}`); return } item.value = response.data; etag.value = response.headers.get('etag') ?? '' } catch (cause) { error.value = errorMessage(cause) } finally { busy.value = false } }
</script>
<template><section v-if="item"><p class="eyebrow">申诉 · {{ item.appeal_id }}</p><h1>{{ item.appeal_status }}</h1><p v-if="error" class="alert error">{{ error }}</p><article class="card"><p>原退款申请 {{ item.refund_id }}</p><p>{{ item.reason }}</p></article><button v-if="!item.claimed && item.appeal_status === 'submitted'" :disabled="busy" @click="claim">领取复核</button><form v-if="item.claimed && item.appeal_status === 'reviewing'" class="card" @submit.prevent><label>复核意见<textarea v-model="reason" required /></label><div class="actions"><button class="danger" :disabled="busy || !reason.trim()" @click="decide('reject')">驳回</button><button :disabled="busy || !reason.trim()" @click="decide('approve')">支持申诉</button></div></form></section><p v-else-if="!error">正在加载…</p></template>
