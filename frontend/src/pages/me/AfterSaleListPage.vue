<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { listMyRefunds, type RefundApplication } from '../../api/after-sales'
import { errorMessage } from '../../api/http'
import { useUserAuthStore } from '../../stores/user-auth'

const auth = useUserAuthStore()
const items = ref<RefundApplication[]>([])
const loading = ref(true)
const error = ref('')
onMounted(async () => {
  try {
    if (!auth.accessToken) throw new Error('missing user token')
    items.value = (await listMyRefunds(auth.accessToken)).data.items
  }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
})
</script>

<template>
  <main class="page-shell">
    <h1>我的售后</h1>
    <p v-if="loading">正在加载…</p>
    <p v-else-if="error" class="alert error">{{ error }}</p>
    <p v-else-if="!items.length" class="muted">暂无售后记录。</p>
    <section v-else class="stack">
      <RouterLink v-for="item in items" :key="item.refund_id" class="card" :to="`/me/after-sales/${item.refund_id}`">
        <strong>{{ item.refund_id }}</strong>
        <span class="badge">{{ item.refund_status }}</span>
        <small>订单 {{ item.order_id }}</small>
        <p>退款金额：¥{{ (Number(item.requested_amount.minor_units) / 100).toFixed(2) }}</p>
      </RouterLink>
    </section>
  </main>
</template>
