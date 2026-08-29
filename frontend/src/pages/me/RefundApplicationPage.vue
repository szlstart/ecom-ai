<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { checkRefundEligibility, createRefundApplication, type RefundEligibility, type RefundEligibilityInput } from '@/api/after-sales'
import { formatMoney } from '@/api/catalog'
import { errorMessage } from '@/api/http'
import { getMyOrder, type OrderDetail } from '@/api/orders'
import { confirmAction } from '@/composables/confirmation'
import { useUserAuthStore } from '@/stores/user-auth'

const route = useRoute()
const router = useRouter()
const auth = useUserAuthStore()
const order = ref<OrderDetail | null>(null)
const selected = ref<Record<string, boolean>>({})
const quantities = ref<Record<string, number>>({})
const refundType = ref<'refund_only' | 'return_and_refund'>('refund_only')
const reasonCode = ref('NO_LONGER_NEEDED')
const reasonDetail = ref('')
const eligibility = ref<RefundEligibility | null>(null)
const error = ref('')
const busy = ref(false)

const input = computed<RefundEligibilityInput | null>(() => {
  if (!order.value) return null
  const items = order.value.items
    .filter((item) => selected.value[item.order_item_id])
    .map((item) => ({ order_item_id: item.order_item_id, quantity: quantities.value[item.order_item_id] ?? 1 }))
  return items.length ? { order_id: order.value.order_id, items, requested_type: refundType.value, reason_code: reasonCode.value } : null
})

async function load() {
  if (!auth.accessToken) return
  try {
    order.value = (await getMyOrder(String(route.params.orderId), auth.accessToken)).data
    for (const item of order.value.items) {
      const available = Math.max(0, item.quantity - item.refunded_quantity)
      selected.value[item.order_item_id] = available > 0
      quantities.value[item.order_item_id] = Math.max(1, available)
    }
  } catch (cause) { error.value = errorMessage(cause) }
}

async function preview() {
  if (!input.value || !auth.accessToken) return
  busy.value = true
  error.value = ''
  try { eligibility.value = (await checkRefundEligibility(input.value, auth.accessToken)).data }
  catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}

async function submit() {
  if (!input.value || !eligibility.value?.eligible || !auth.accessToken) return
  if (!await confirmAction(`确认提交 ${formatMoney(eligibility.value.suggested_refund_amount)} 的售后申请？`, { title: '提交售后申请', confirmText: '确认提交' })) return
  busy.value = true
  try {
    const result = await createRefundApplication(input.value, eligibility.value, reasonDetail.value.trim() || null, auth.accessToken)
    await router.replace(`/me/after-sales/${result.data.refund_id}`)
  } catch (cause) {
    error.value = errorMessage(cause)
    eligibility.value = null
  } finally { busy.value = false }
}

onMounted(load)
</script>

<template>
  <main class="page-shell">
    <h1>申请售后</h1>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <form v-if="order" class="stack" @submit.prevent="eligibility ? submit() : preview()">
      <section class="card stack">
        <h2>选择商品和数量</h2>
        <label v-for="item in order.items" :key="item.order_item_id">
          <input v-model="selected[item.order_item_id]" type="checkbox" />
          {{ item.product_name }} · 款式：{{ item.sku_name }}
          <input v-model.number="quantities[item.order_item_id]" type="number" min="1" :max="item.quantity - item.refunded_quantity" :disabled="!selected[item.order_item_id]" />
        </label>
      </section>
      <section class="card stack">
        <label>售后类型<select v-model="refundType"><option value="refund_only">仅退款</option><option value="return_and_refund">退货退款</option></select></label>
        <label>申请原因<select v-model="reasonCode"><option value="NO_LONGER_NEEDED">不再需要</option><option value="QUALITY_ISSUE">质量问题</option><option value="WRONG_ITEM">商品错发</option></select></label>
        <label>补充说明<textarea v-model="reasonDetail" maxlength="500" /></label>
      </section>
      <section v-if="eligibility" class="card stack" aria-live="polite">
        <h2>资格检查结果</h2>
        <p v-if="eligibility.eligible">预计退款：<strong>{{ formatMoney(eligibility.suggested_refund_amount) }}</strong></p>
        <p v-else class="alert error">当前不可申请：{{ eligibility.blocking_reasons.join('、') }}</p>
        <small>资格结果有效至 {{ new Date(eligibility.expires_at).toLocaleString('zh-CN') }}，提交时服务端会再次校验。</small>
      </section>
      <button type="submit" :disabled="busy || !input || (eligibility !== null && !eligibility.eligible)">{{ busy ? '处理中…' : eligibility ? '确认提交申请' : '检查退款资格' }}</button>
      <button v-if="eligibility" type="button" :disabled="busy" @click="eligibility = null">返回修改</button>
    </form>
    <p v-else-if="!error">正在加载订单…</p>
  </main>
</template>
