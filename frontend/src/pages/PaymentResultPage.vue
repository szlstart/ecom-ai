<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { formatMoney } from '@/api/catalog'
import { errorMessage } from '@/api/http'
import { closePayment, getPayment, type Payment } from '@/api/payments'
import PageState from '@/components/PageState.vue'
import { useUserAuthStore } from '@/stores/user-auth'

const route = useRoute(), auth = useUserAuthStore(), payment = ref<Payment | null>(null)
const loading = ref(true), closing = ref(false), error = ref(''), attempts = ref(0)
let timer: number | undefined
const terminal = computed(() => payment.value && !['created', 'pending'].includes(payment.value.payment_status))
const title = computed(() => ({ confirming: '支付结果确认中', succeeded: '支付成功', failed: '支付失败', closed: '支付已关闭', refunded: '支付已退款' }[payment.value?.display_status ?? 'confirming']))
function token() { if (!auth.accessToken) throw new Error('missing user token'); return auth.accessToken }
async function load() {
  try {
    payment.value = (await getPayment(String(route.params.paymentId), token())).data
    error.value = ''; attempts.value += 1
    if (terminal.value || attempts.value >= 15) stop()
  } catch (cause) { error.value = errorMessage(cause); stop() }
  finally { loading.value = false }
}
function stop() { if (timer !== undefined) window.clearInterval(timer); timer = undefined }
async function closeAttempt() {
  if (!payment.value || closing.value || terminal.value) return
  if (!window.confirm('确定关闭当前支付尝试吗？关闭后可返回收银台重新发起。')) return
  closing.value = true; error.value = ''; stop()
  try { payment.value = (await closePayment(payment.value, token())).data }
  catch (cause) { error.value = errorMessage(cause); await load() }
  finally { closing.value = false }
}
onMounted(async () => { await load(); if (!terminal.value && attempts.value < 15) timer = window.setInterval(load, 2000) })
onBeforeUnmount(stop)
</script>

<template>
  <section class="content-section payment-bridge-page">
    <PageState :loading="loading" :error="error" :empty="!payment" empty-title="支付单不可用" empty-message="请返回订单列表重试。" @retry="load">
      <article v-if="payment" class="card" aria-live="polite">
        <p class="eyebrow">支付结果</p><h1>{{ title }}</h1>
        <p>支付单 <strong>{{ payment.payment_id }}</strong></p>
        <p>支付金额 <strong>{{ formatMoney(payment.requested_amount) }}</strong></p>
        <div v-if="payment.display_status === 'confirming'" class="notice info">渠道尚未给出可信终态。系统将有限轮询；离开本页不会取消支付，也不会重复创建订单。</div>
        <div class="actions"><button v-if="payment.display_status === 'confirming'" type="button" class="secondary" :disabled="closing" @click="closeAttempt">{{ closing ? '正在关闭…' : '关闭本次支付' }}</button><RouterLink :to="`/pay/${payment.trade_order_id}`">返回收银台</RouterLink><RouterLink to="/me/orders?view=all">查看订单</RouterLink></div>
      </article>
    </PageState>
  </section>
</template>
