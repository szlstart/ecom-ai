<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { cancelRefund, createRefundAppeal, getMyRefund, listRefundEvents, type RefundApplication, type RefundEvent } from '../../api/after-sales'
import { formatMoney } from '../../api/catalog'
import { errorMessage } from '../../api/http'
import { useUserAuthStore } from '../../stores/user-auth'
import { confirmAction } from '@/composables/confirmation'

const route = useRoute()
const router = useRouter()
const auth = useUserAuthStore()
const item = ref<RefundApplication | null>(null)
const events = ref<RefundEvent[]>([])
const error = ref('')
const busy = ref(false)
const appealReason = ref('')

async function load() {
  if (!auth.accessToken) return
  error.value = ''
  try {
    const refundId = String(route.params.refundId)
    const [detail, history] = await Promise.all([
      getMyRefund(refundId, auth.accessToken),
      listRefundEvents(refundId, auth.accessToken),
    ])
    item.value = detail.data
    events.value = history.data.items
  } catch (cause) { error.value = errorMessage(cause) }
}

async function cancel() {
  if (!item.value || !auth.accessToken || !await confirmAction('确定撤销售后申请吗？', { title: '撤销售后申请', confirmText: '确认撤销', tone: 'danger' })) return
  busy.value = true
  try {
    await cancelRefund(item.value.refund_id, '用户主动撤销售后申请', auth.accessToken)
    await load()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}

async function appeal() {
  if (!item.value || !auth.accessToken || appealReason.value.trim().length < 2) return
  busy.value = true
  try {
    const result = await createRefundAppeal(item.value.refund_id, appealReason.value.trim(), auth.accessToken)
    await router.push(`/me/appeals/${result.data.appeal_id}`)
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}

onMounted(load)
</script>

<template>
  <main class="page-shell">
    <p v-if="error" class="alert error">{{ error }}</p>
    <section v-else-if="item" class="card">
      <h1>售后详情</h1>
      <p>售后单：{{ item.refund_id }}</p>
      <p>订单：{{ item.order_id }}</p>
      <p>状态：<span class="badge">{{ item.refund_status }}</span></p>
      <p>原因：{{ item.reason_code }} {{ item.reason_detail }}</p>
      <p>申请金额：{{ formatMoney(item.requested_amount) }}</p>
      <p>批准金额：{{ formatMoney(item.approved_amount) }}</p>
      <h2>申请商品</h2>
      <ul><li v-for="line in item.items" :key="line.order_item_id">{{ line.order_item_id }} × {{ line.quantity }} · {{ formatMoney(line.requested_amount) }}</li></ul>
      <div class="stack">
        <RouterLink v-if="item.refund_status === 'waiting_return' || item.refund_status === 'returning'" class="button" :to="`/me/after-sales/${item.refund_id}/return-shipment`">填写退货物流</RouterLink>
        <button v-if="item.available_actions.includes('cancel')" type="button" :disabled="busy" @click="cancel">撤销申请</button>
        <RouterLink v-if="item.available_actions.includes('create_new_refund_application')" class="button" :to="`/me/orders/${item.order_id}/refund`">重新申请</RouterLink>
      </div>
      <form v-if="item.available_actions.includes('create_refund_appeal')" class="stack" @submit.prevent="appeal">
        <h2>申请平台复核</h2>
        <label>申诉说明<textarea v-model="appealReason" required minlength="2" maxlength="1000" /></label>
        <button type="submit" :disabled="busy || appealReason.trim().length < 2">提交申诉</button>
      </form>
      <h2>处理进度</h2>
      <ol class="timeline">
        <li v-for="event in events" :key="event.event_id">
          <strong>{{ event.event_code }}</strong>
          <time :datetime="event.occurred_at">{{ new Date(event.occurred_at).toLocaleString('zh-CN') }}</time>
        </li>
      </ol>
    </section>
    <p v-else>正在加载…</p>
  </main>
</template>
