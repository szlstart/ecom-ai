<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { listAdminRefunds } from '@/api/admin-after-sales'
import type { RefundApplication } from '@/api/after-sales'
import { formatMoney } from '@/api/catalog'
import { errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
const auth = useAdminAuthStore(), items = ref<RefundApplication[]>([]), error = ref(''), nextCursor = ref<string | null>(null), loadingMore = ref(false)
async function load() { try { const response = await listAdminRefunds(auth.accessToken!); items.value = response.data.items; nextCursor.value = response.data.next_cursor } catch (cause) { error.value = errorMessage(cause) } }
async function loadMore() { if (!nextCursor.value || loadingMore.value) return; loadingMore.value = true; try { const response = await listAdminRefunds(auth.accessToken!, nextCursor.value); items.value.push(...response.data.items); nextCursor.value = response.data.next_cursor } catch (cause) { error.value = errorMessage(cause) } finally { loadingMore.value = false } }
onMounted(load)
</script>
<template><section><p class="eyebrow">售后治理</p><h1>退款申请</h1><p v-if="error" class="alert error">{{ error }}</p><div class="stack"><RouterLink v-for="item in items" :key="item.refund_id" class="card" :to="`/admin/refund-applications/${item.refund_id}`"><strong>{{ item.refund_id }}</strong><span class="badge">{{ item.refund_status }}</span><p>{{ formatMoney(item.requested_amount) }} · 订单 {{ item.order_id }}</p></RouterLink><p v-if="!items.length && !error">暂无退款申请。</p><button v-if="nextCursor" type="button" class="secondary" :disabled="loadingMore" @click="loadMore">{{ loadingMore ? '正在加载…' : '加载更多退款申请' }}</button></div></section></template>
