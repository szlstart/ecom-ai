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
  <section class="payment-bridge-page">
    <header class="page-heading"><div><p class="eyebrow">安全收银台</p><h1>支付订单</h1></div><RouterLink to="/me/orders?view=pending_payment">返回订单</RouterLink></header>
    <PageState :loading="loading" :error="error" :empty="!trade" empty-title="交易单不可用" empty-message="请返回订单列表重试。" @retry="load">
      <div v-if="trade" class="payment-layout">
        <article class="payment-order-card">
          <div class="payment-order-heading"><span>订单已创建</span><strong>请完成支付</strong></div>
          <dl><div><dt>交易单号</dt><dd>{{ trade.trade_order_id }}</dd></div><div><dt>店铺订单</dt><dd>{{ trade.order_count }} 个</dd></div><div><dt>支付状态</dt><dd>等待支付</dd></div></dl>
          <div class="payment-security-list"><div><b>1</b><span><strong>服务端锁定金额</strong><small>支付金额来自订单快照，前端无法篡改。</small></span></div><div><b>2</b><span><strong>事务内完成扣款</strong><small>用户余额、支付单和订单状态会以事务方式一致更新。</small></span></div><div><b>3</b><span><strong>收货后结算给商家</strong><small>付款后资金处于待结算状态，确认收货后才计入店铺营业额。</small></span></div></div>
        </article>
        <aside class="payment-pay-card">
          <p class="eyebrow">支付金额</p><p class="payment-total"><span>应付</span><strong>{{ formatMoney(trade.amounts.payable_amount) }}</strong></p>
          <div class="payment-method"><span class="wallet-symbol">¥</span><span><strong>商城账户余额</strong><small>模拟余额支付 · 即时到账</small></span><b>已选择</b></div>
          <button type="button" :disabled="submitting || trade.trade_status !== 'pending_payment'" @click="pay">{{ submitting ? '正在扣减余额并支付…' : `确认支付 ${formatMoney(trade.amounts.payable_amount)}` }}</button>
          <p>当前为模拟支付环境，不调用微信或支付宝，也不会产生真实资金交易。点击后会扣减商城模拟余额。</p>
        </aside>
      </div>
    </PageState>
  </section>
</template>
