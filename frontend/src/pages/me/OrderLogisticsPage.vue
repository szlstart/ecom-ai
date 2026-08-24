<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { errorMessage } from '@/api/http'
import { listOrderShipments, type ShipmentSummary } from '@/api/logistics'
import { useUserAuthStore } from '@/stores/user-auth'
const route = useRoute(), auth = useUserAuthStore(), items = ref<ShipmentSummary[]>([]), error = ref('')
function estimate(item: ShipmentSummary) { const value = item.delivery_estimate; return value.status === 'available' && value.min_at && value.max_at ? `${new Date(value.min_at).toLocaleDateString('zh-CN')}—${new Date(value.max_at).toLocaleDateString('zh-CN')}` : '预计送达时间暂不可用' }
onMounted(async () => { try { items.value = (await listOrderShipments(String(route.params.orderId), auth.accessToken!)).data.items } catch (cause) { error.value = errorMessage(cause) } })
</script>
<template><main class="page-shell"><h1>订单物流</h1><p v-if="error" class="alert error">{{ error }}</p><div class="stack"><RouterLink v-for="item in items" :key="item.shipment_id" class="card" :to="`/me/shipments/${item.shipment_id}`"><strong>{{ item.carrier_name }} · {{ item.tracking_no_masked }}</strong><span class="badge">{{ item.shipment_status }}</span><p>{{ item.items.map((line) => `${line.product_name} × ${line.quantity}`).join('、') }}</p><p>{{ item.last_track?.description || '暂无物流轨迹' }}</p><small>{{ estimate(item) }} · {{ item.delivery_estimate.disclaimer }}</small></RouterLink><p v-if="!items.length && !error">商家正在备货，暂无物流包裹。</p></div></main></template>
