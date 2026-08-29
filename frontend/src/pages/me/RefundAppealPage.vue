<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { getRefundAppeal, type RefundAppeal } from '@/api/after-sales'
import { errorMessage } from '@/api/http'
import { useUserAuthStore } from '@/stores/user-auth'

const route = useRoute()
const auth = useUserAuthStore()
const appeal = ref<RefundAppeal | null>(null)
const error = ref('')

onMounted(async () => {
  if (!auth.accessToken) return
  try { appeal.value = (await getRefundAppeal(String(route.params.appealId), auth.accessToken)).data }
  catch (cause) { error.value = errorMessage(cause) }
})
</script>

<template>
  <main class="page-shell">
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <section v-else-if="appeal" class="card stack">
      <h1>售后申诉</h1>
      <p>申诉单：{{ appeal.appeal_id }}</p>
      <p>状态：<span class="badge">{{ appeal.appeal_status }}</span></p>
      <p>{{ appeal.reason }}</p>
      <p>提交时间：{{ new Date(appeal.submitted_at).toLocaleString('zh-CN') }}</p>
      <RouterLink :to="`/me/after-sales/${appeal.refund_id}`">返回售后详情</RouterLink>
    </section>
    <p v-else-if="!error">正在加载申诉…</p>
  </main>
</template>
