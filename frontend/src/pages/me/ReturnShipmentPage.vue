<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getMyRefund, upsertReturnShipment, type RefundApplication } from '../../api/after-sales'
import { errorMessage } from '../../api/http'
import { useUserAuthStore } from '../../stores/user-auth'

const route = useRoute()
const router = useRouter()
const auth = useUserAuthStore()
const refund = ref<RefundApplication | null>(null)
const carrierCode = ref('fake_express')
const trackingNo = ref('')
const error = ref('')
const saving = ref(false)

onMounted(async () => {
  try {
    if (!auth.accessToken) throw new Error('missing user token')
    refund.value = (await getMyRefund(String(route.params.refundId), auth.accessToken)).data
  }
  catch (cause) { error.value = errorMessage(cause) }
})

async function submit() {
  if (!refund.value || !auth.accessToken) return
  saving.value = true
  error.value = ''
  try {
    await upsertReturnShipment(refund.value.refund_id, { carrier_code: carrierCode.value, tracking_no: trackingNo.value }, refund.value.version, auth.accessToken)
    await router.push(`/me/after-sales/${refund.value.refund_id}`)
  } catch (cause) { error.value = errorMessage(cause) }
  finally { saving.value = false }
}
</script>

<template>
  <main class="page-shell">
    <section class="card">
      <h1>填写退货物流</h1>
      <p v-if="error" class="alert error">{{ error }}</p>
      <form v-if="refund" class="stack" @submit.prevent="submit">
        <label>承运商代码<input v-model="carrierCode" required pattern="[a-z][a-z0-9_]+" /></label>
        <label>运单号<input v-model="trackingNo" required minlength="6" maxlength="64" autocomplete="off" /></label>
        <button type="submit" :disabled="saving">{{ saving ? '提交中…' : '提交物流' }}</button>
      </form>
      <p v-else-if="!error">正在加载…</p>
    </section>
  </main>
</template>
