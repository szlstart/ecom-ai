<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { listAdminRefunds } from '@/api/admin-after-sales'
import type { RefundApplication } from '@/api/after-sales'
import { formatMoney } from '@/api/catalog'
import { errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
const auth = useAdminAuthStore(), items = ref<RefundApplication[]>([]), error = ref('')
onMounted(async () => { try { items.value = (await listAdminRefunds(auth.accessToken!)).data.items } catch (cause) { error.value = errorMessage(cause) } })
</script>
<template><section><p class="eyebrow">售后治理</p><h1>退款申请</h1><p v-if="error" class="alert error">{{ error }}</p><div class="stack"><RouterLink v-for="item in items" :key="item.refund_id" class="card" :to="`/admin/refund-applications/${item.refund_id}`"><strong>{{ item.refund_id }}</strong><span class="badge">{{ item.refund_status }}</span><p>{{ formatMoney(item.requested_amount) }} · 订单 {{ item.order_id }}</p></RouterLink><p v-if="!items.length && !error">暂无退款申请。</p></div></section></template>
