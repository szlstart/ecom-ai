<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'

import { formatMoney } from '@/api/catalog'
import { errorMessage } from '@/api/http'
import { getAdminPayment, reconcileAdminPayment, type AdminPayment } from '@/api/payments'
import { useAdminAuthStore } from '@/stores/admin-auth'

const route = useRoute(), auth = useAdminAuthStore()
const item = ref<AdminPayment | null>(null), etag = ref(''), error = ref(''), notice = ref(''), busy = ref(false)
const form = reactive({ reason_code: 'PAYMENT_STATUS_RECONCILIATION', reason: '' })
const canReconcile = computed(() => auth.has('payments:reconcile') && item.value?.available_admin_actions.includes('reconcile'))
function dateTime(value: string | null): string { return value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : '—' }
function accept(response: { data: AdminPayment; headers: Headers }) { item.value = response.data; etag.value = response.headers.get('etag') ?? '' }
async function load() { accept(await getAdminPayment(String(route.params.paymentId), auth.accessToken!)) }
async function reconcile() { if (!item.value || !form.reason.trim()) return; busy.value = true; error.value = ''; notice.value = ''; try { const response = await reconcileAdminPayment(item.value.payment.payment_id, etag.value, form.reason_code, form.reason.trim(), auth.accessToken!); item.value = response.data.payment; etag.value = response.headers.get('etag') ?? ''; notice.value = `渠道权威状态：${response.data.provider_status}；本地处理：${response.data.result === 'status_updated' ? '状态已更新' : '无需变更'}。`; form.reason = '' } catch (cause) { error.value = errorMessage(cause) } finally { busy.value = false } }
onMounted(() => load().catch((cause) => { error.value = errorMessage(cause) }))
</script>

<template><section v-if="item" class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">支付单 · {{ item.payment.payment_id }}</p><h1>{{ item.payment.payment_status }}</h1><p class="muted">交易 {{ item.payment.trade_order_id }} · 用户 {{ item.user_id }}</p></div></header><p v-if="error" class="alert error" role="alert">{{ error }}</p><p v-if="notice" class="alert success" role="status">{{ notice }}</p><div class="settings-grid"><article class="card"><h2>渠道事实</h2><dl class="detail-list"><dt>渠道/方式</dt><dd>{{ item.payment.provider }} / {{ item.payment.payment_method }}</dd><dt>渠道交易号</dt><dd>{{ item.provider_trade_no_masked || '—' }}</dd><dt>数据范围</dt><dd>{{ item.store_ids.join('、') }}</dd><dt>本地版本</dt><dd>v{{ item.payment.version }}</dd><dt>到期时间</dt><dd>{{ dateTime(item.payment.expires_at) }}</dd></dl></article><article class="card"><h2>金额</h2><dl class="detail-list"><dt>请求</dt><dd>{{ formatMoney(item.payment.requested_amount) }}</dd><dt>已支付</dt><dd>{{ formatMoney(item.payment.paid_amount) }}</dd><dt>已退款</dt><dd>{{ formatMoney(item.payment.refunded_amount) }}</dd><dt>支付时间</dt><dd>{{ dateTime(item.payment.paid_at) }}</dd><dt>关闭时间</dt><dd>{{ dateTime(item.payment.closed_at) }}</dd></dl></article></div><form v-if="canReconcile" class="card" @submit.prevent="reconcile"><h2>发起渠道对账</h2><p>此操作只按当前支付单的渠道引用、金额和币种查询，不允许输入或指定目标支付状态。渠道身份或金额不一致时自动处理将被拒绝。</p><label>原因码<input v-model.trim="form.reason_code" required pattern="[A-Z][A-Z0-9_]{1,63}" /></label><label>对账原因<textarea v-model.trim="form.reason" required minlength="2" maxlength="500" /></label><button :disabled="busy || !form.reason">{{ busy ? '查询中…' : '查询渠道权威状态' }}</button></form><article class="card"><h2>不可变支付事件</h2><ol class="timeline"><li v-for="event in item.payment.events" :key="event.event_id"><strong>{{ event.event_type }}</strong><p>{{ event.from_status ?? '创建' }} → {{ event.to_status }} · {{ event.source_type }}</p><time>{{ dateTime(event.occurred_at) }}</time></li></ol><p v-if="!item.payment.events.length">暂无事件。</p></article></section><p v-else-if="!error">正在加载…</p><p v-else class="alert error" role="alert">{{ error }}</p></template>
