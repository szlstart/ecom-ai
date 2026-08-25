<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { formatMoney } from '@/api/catalog'
import { errorMessage } from '@/api/http'
import { listAdminOrders, type AdminOrderSummary } from '@/api/orders'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const items = ref<AdminOrderSummary[]>([])
const loading = ref(true)
const error = ref('')
const filters = reactive({ q: '', order_status: '', payment_status: '', fulfillment_status: '', after_sale_status: '' })

async function load() {
  loading.value = true
  error.value = ''
  try { items.value = (await listAdminOrders(filters, auth.accessToken!)).data.items }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

function dateTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(value))
}

onMounted(load)
</script>

<template>
  <section class="admin-page-stack">
    <header class="page-heading"><div><p class="eyebrow">交易治理</p><h1>订单管理</h1><p class="muted">结果按当前平台或店铺数据范围过滤；用户标识默认脱敏。</p></div></header>
    <form class="filter-bar" @submit.prevent="load">
      <label>订单/交易/店铺/用户 ID<input v-model.trim="filters.q" maxlength="64" /></label>
      <label>订单状态<input v-model.trim="filters.order_status" maxlength="32" /></label>
      <label>支付状态<input v-model.trim="filters.payment_status" maxlength="32" /></label>
      <label>履约状态<input v-model.trim="filters.fulfillment_status" maxlength="32" /></label>
      <label>售后状态<input v-model.trim="filters.after_sale_status" maxlength="32" /></label>
      <button>查询</button>
    </form>
    <PageState :loading="loading" :error="error" :empty="!loading && !error && items.length === 0" empty-title="没有匹配订单" @retry="load">
      <div class="table-wrap"><table><thead><tr><th>订单</th><th>店铺/用户</th><th>状态</th><th>金额</th><th>创建时间</th><th>操作</th></tr></thead><tbody>
        <tr v-for="item in items" :key="item.order.order_id">
          <td><strong>{{ item.order.order_id }}</strong><small>交易 {{ item.order.trade_order_id }}</small></td>
          <td>{{ item.order.store.store_name }}<small>{{ item.user_name_masked }} · {{ item.user_id }}</small></td>
          <td><span class="badge">{{ item.order.order_status }}</span><small>{{ item.order.payment_status }} / {{ item.order.fulfillment_status }} / {{ item.order.after_sale_status }}</small></td>
          <td>{{ formatMoney(item.order.amounts.payable_amount) }}<small>调整 {{ formatMoney(item.order.amounts.adjustment_amount) }}</small></td>
          <td>{{ dateTime(item.order.created_at) }}</td>
          <td><RouterLink :to="`/admin/orders/${item.order.order_id}`">查看详情</RouterLink></td>
        </tr>
      </tbody></table></div>
    </PageState>
  </section>
</template>
