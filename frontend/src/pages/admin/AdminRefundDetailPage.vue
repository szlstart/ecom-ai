<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { claimAdminRefund, decideAdminRefund, getAdminRefund, isApprovalRequired } from '@/api/admin-after-sales'
import type { RefundApplication } from '@/api/after-sales'
import { formatMoney } from '@/api/catalog'
import { errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
const route = useRoute(), router = useRouter(), auth = useAdminAuthStore(), item = ref<RefundApplication | null>(null), etag = ref(''), reason = ref(''), error = ref(''), busy = ref(false)
async function load() { const response = await getAdminRefund(String(route.params.refundId), auth.accessToken!); item.value = response.data; etag.value = response.headers.get('etag') ?? '' }
onMounted(() => load().catch((cause) => { error.value = errorMessage(cause) }))
async function claim() { busy.value = true; try { const response = await claimAdminRefund(item.value!.refund_id, etag.value, auth.accessToken!); item.value = response.data; etag.value = response.headers.get('etag') ?? '' } catch (cause) { error.value = errorMessage(cause) } finally { busy.value = false } }
async function decide(decision: 'approve' | 'reject') { if (!reason.value.trim()) return; busy.value = true; try { const response = await decideAdminRefund(item.value!.refund_id, etag.value, decision, decision === 'approve' ? 'POLICY_PASSED' : 'POLICY_NOT_MET', reason.value.trim(), auth.accessToken!); if (isApprovalRequired(response.data)) { await router.push(`/admin/approval-requests/${response.data.approval_request_id}`); return } item.value = response.data; etag.value = response.headers.get('etag') ?? '' } catch (cause) { error.value = errorMessage(cause) } finally { busy.value = false } }
</script>
<template><section v-if="item"><p class="eyebrow">退款申请 · {{ item.refund_id }}</p><h1>{{ item.refund_status }}</h1><p v-if="error" class="alert error">{{ error }}</p><div class="settings-grid"><article class="card"><h2>申请信息</h2><p>订单 {{ item.order_id }}</p><p>{{ item.reason_code }} · {{ item.reason_detail }}</p><p>申请 {{ formatMoney(item.requested_amount) }}</p></article><article class="card"><h2>商品明细</h2><p v-for="line in item.items" :key="line.order_item_id">{{ line.order_item_id }} × {{ line.quantity }} · {{ formatMoney(line.requested_amount) }}</p></article></div><button v-if="!item.claimed && ['submitted','merchant_review'].includes(item.refund_status)" :disabled="busy" @click="claim">领取审核</button><form v-if="item.claimed && ['submitted','merchant_review'].includes(item.refund_status)" class="card" @submit.prevent><label>审核理由<textarea v-model="reason" required minlength="2" maxlength="500" /></label><div class="actions"><button class="danger" :disabled="busy || !reason.trim()" @click="decide('reject')">拒绝</button><button :disabled="busy || !reason.trim()" @click="decide('approve')">批准</button></div></form></section><p v-else-if="!error">正在加载…</p></template>
