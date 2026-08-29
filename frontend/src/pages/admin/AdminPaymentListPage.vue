<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { formatMoney } from '@/api/catalog'
import { errorMessage } from '@/api/http'
import { listAdminPayments, type AdminPayment } from '@/api/payments'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const items = ref<AdminPayment[]>([])
const loading = ref(true)
const error = ref('')
const filters = reactive({ q: '', payment_status: '', provider: '' })
async function load() { loading.value = true; error.value = ''; try { items.value = (await listAdminPayments(filters, auth.accessToken!)).data.items } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false } }
function dateTime(value: string): string { return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(value)) }
onMounted(load)
</script>

<template><section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">资金事实</p><h1>支付管理</h1><p class="muted">页面只读展示支付尝试；状态变更只能来自验签回调或渠道权威查询。</p></div></header><form class="filter-bar" @submit.prevent="load"><label>支付单/交易单<input v-model.trim="filters.q" maxlength="64" /></label><label>支付状态<input v-model.trim="filters.payment_status" maxlength="32" /></label><label>渠道<input v-model.trim="filters.provider" maxlength="32" /></label><button>查询</button></form><PageState :loading="loading" :error="error" :empty="!loading && !error && items.length === 0" empty-title="没有匹配支付单" @retry="load"><div class="table-wrap"><table><thead><tr><th>支付单</th><th>交易/用户</th><th>范围</th><th>渠道</th><th>状态</th><th>金额</th><th>到期</th><th>操作</th></tr></thead><tbody><tr v-for="item in items" :key="item.payment.payment_id"><td><strong>{{ item.payment.payment_id }}</strong></td><td>{{ item.payment.trade_order_id }}<small>{{ item.user_id }}</small></td><td>{{ item.store_ids.join('、') }}</td><td>{{ item.payment.provider }}<small>{{ item.provider_trade_no_masked || '尚无渠道引用' }}</small></td><td><span class="badge">{{ item.payment.payment_status }}</span></td><td>{{ formatMoney(item.payment.requested_amount) }}</td><td>{{ dateTime(item.payment.expires_at) }}</td><td><RouterLink :to="`/admin/payments/${item.payment.payment_id}`">查看详情</RouterLink></td></tr></tbody></table></div></PageState></section></template>
