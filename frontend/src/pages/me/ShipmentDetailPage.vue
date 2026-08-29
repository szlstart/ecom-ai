<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { errorMessage } from '@/api/http'
import { getShipment, listShipmentTracks, refreshShipment, type ShipmentDetail, type ShipmentTrack } from '@/api/logistics'
import { useUserAuthStore } from '@/stores/user-auth'
const route = useRoute(), auth = useUserAuthStore(), item = ref<ShipmentDetail | null>(null), tracks = ref<ShipmentTrack[]>([]), error = ref(''), message = ref(''), busy = ref(false)
async function load() { const id = String(route.params.shipmentId); const [detail, history] = await Promise.all([getShipment(id, auth.accessToken!), listShipmentTracks(id, auth.accessToken!)]); item.value = detail.data; tracks.value = history.data.items }
onMounted(() => load().catch((cause) => { error.value = errorMessage(cause) }))
async function copyTracking() { if (!item.value) return; await navigator.clipboard.writeText(item.value.tracking_no); message.value = '完整运单号已复制。' }
async function refresh() { if (!item.value) return; busy.value = true; try { await refreshShipment(item.value.shipment_id, auth.accessToken!); message.value = '已提交物流刷新，请稍后查看。' } catch (cause) { error.value = errorMessage(cause) } finally { busy.value = false } }
</script>
<template><main v-if="item" class="page-shell"><h1>物流详情</h1><p v-if="error" class="alert error">{{ error }}</p><p v-if="message" class="alert success">{{ message }}</p><article class="card"><h2>{{ item.carrier_name }}</h2><p>运单号 {{ item.tracking_no }} <button class="link-button" @click="copyTracking">复制</button></p><p>状态：{{ item.shipment_status }}</p><button :disabled="busy" @click="refresh">刷新物流</button><small>完整运单号仅在本人鉴权后的详情页展示；客服、消息和日志仅使用掩码。</small></article><article class="card"><h2>物流轨迹</h2><ol class="timeline"><li v-for="track in tracks" :key="`${track.occurred_at}-${track.description}`"><strong>{{ track.description }}</strong><p>{{ track.location_text }}</p><time>{{ new Date(track.occurred_at).toLocaleString('zh-CN') }}</time></li></ol><p v-if="!tracks.length">暂无物流轨迹。</p></article></main><p v-else-if="!error">正在加载…</p></template>
