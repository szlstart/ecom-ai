<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { formatMoney } from '@/api/catalog'
import { errorMessage } from '@/api/http'
import { getMyTradeOrder, type TradeOrder } from '@/api/orders'
import { createPayment, listTradePayments } from '@/api/payments'
import PageState from '@/components/PageState.vue'
import { useUserAuthStore } from '@/stores/user-auth'

const route = useRoute(), router = useRouter(), auth = useUserAuthStore()
const trade = ref<TradeOrder | null>(null), loading = ref(true), submitting = ref(false), error = ref('')
const tradeOrderId = String(route.params.tradeOrderId)
function token() { if (!auth.accessToken) throw new Error('missing user token'); return auth.accessToken }
async function load() {
  loading.value = true; error.value = ''
  try {
    trade.value = (await getMyTradeOrder(tradeOrderId, token())).data
    const attempts = (await listTradePayments(tradeOrderId, token())).data.items
    const active = attempts.find((item) => ['created', 'pending'].includes(item.payment_status))
    if (active) await router.replace(`/payments/${active.payment_id}/result`)
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}
async function pay() {
  if (submitting.value) return
  submitting.value = true; error.value = ''
  try {
    const payment = (await createPayment(tradeOrderId, token())).data
    await router.push(payment.action?.url ?? `/payments/${payment.payment_id}/result`)
  } catch (cause) { error.value = errorMessage(cause); await load() }
  finally { submitting.value = false }
}
onMounted(load)
</script>

<template>
  <section class="content-section payment-bridge-page">
    <header class="page-heading"><div><p class="eyebrow">安全收银台</p><h1>支付订单</h1></div><RouterLink to="/me/orders?view=pending_payment">返回订单</RouterLink></header>
    <PageState :loading="loading" :error="error" :empty="!trade" empty-title="交易单不可用" empty-message="请返回订单列表重试。" @retry="load">
      <article v-if="trade" class="card">
        <p>合并交易单 <strong>{{ trade.trade_order_id }}</strong></p>
        <p class="payment-total">应付 <strong>{{ formatMoney(trade.amounts.payable_amount) }}</strong></p>
        <p class="muted">包含 {{ trade.order_count }} 个店铺订单。金额由服务端订单快照确定，页面不能修改。</p>
        <div class="notice info">开发环境使用 Fake Provider。发起后进入支付结果确认页，不会仅凭渠道返回页面判定成功。</div>
        <button type="button" :disabled="submitting || trade.trade_status !== 'pending_payment'" @click="pay">{{ submitting ? '正在创建支付单…' : '使用测试支付渠道' }}</button>
      </article>
    </PageState>
  </section>
</template>
