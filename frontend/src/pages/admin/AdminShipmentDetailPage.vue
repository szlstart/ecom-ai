<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  correctAdminShipmentTracking,
  getAdminShipment,
  refreshAdminShipment,
  type AdminShipmentDetail,
  voidAdminShipment,
} from '@/api/logistics'
import { errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'

const route = useRoute()
const auth = useAdminAuthStore()
const item = ref<AdminShipmentDetail | null>(null)
const etag = ref('')
const trackingNo = ref('')
const correctionReasonCode = ref('TRACKING_INPUT_ERROR')
const correctionReason = ref('')
const voidReasonCode = ref('SHIPMENT_CREATED_IN_ERROR')
const voidReason = ref('')
const error = ref('')
const notice = ref('')
const busy = ref(false)

const canCorrect = computed(() => auth.has('shipments:correct') && item.value?.shipment_status === 'created')
const canVoid = computed(() => auth.has('shipments:void') && item.value?.shipment_status === 'created')

function dateTime(value: string | null): string {
  return value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : '—'
}

async function load() {
  const response = await getAdminShipment(String(route.params.shipmentId), auth.accessToken!)
  item.value = response.data
  etag.value = response.headers.get('etag') ?? ''
}

async function mutate(action: () => Promise<{ data: AdminShipmentDetail; headers: Headers }>, success: string) {
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    const response = await action()
    item.value = response.data
    etag.value = response.headers.get('etag') ?? ''
    notice.value = success
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    busy.value = false
  }
}

async function correctTracking() {
  if (!item.value || !trackingNo.value.trim() || !correctionReason.value.trim()) return
  await mutate(
    () => correctAdminShipmentTracking(item.value!.shipment_id, etag.value, trackingNo.value.trim(), correctionReasonCode.value, correctionReason.value.trim(), auth.accessToken!),
    '运单号已更正，操作已写入审计记录。',
  )
  if (!error.value) {
    trackingNo.value = ''
    correctionReason.value = ''
  }
}

async function voidShipment() {
  if (!item.value || !voidReason.value.trim()) return
  await mutate(
    () => voidAdminShipment(item.value!.shipment_id, etag.value, voidReasonCode.value, voidReason.value.trim(), auth.accessToken!),
    '包裹已作废，订单履约状态已重新计算。',
  )
  if (!error.value) voidReason.value = ''
}

async function refreshTracking() {
  if (!item.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    await refreshAdminShipment(item.value.shipment_id, auth.accessToken!)
    notice.value = '物流同步任务已进入队列，请稍后刷新页面查看结果。'
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    busy.value = false
  }
}

onMounted(() => load().catch((cause) => { error.value = errorMessage(cause) }))
</script>

<template>
  <section v-if="item">
    <p class="eyebrow">物流包裹 · {{ item.shipment_id }}</p>
    <h1>{{ item.carrier_name }} · {{ item.shipment_status }}</h1>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <p v-if="notice" class="alert" role="status">{{ notice }}</p>

    <div class="settings-grid">
      <article class="card">
        <h2>包裹信息</h2>
        <dl class="detail-list">
          <dt>订单</dt><dd>{{ item.order_id }}</dd>
          <dt>店铺</dt><dd>{{ item.store_id }}</dd>
          <dt>承运商</dt><dd>{{ item.carrier_name }}（{{ item.carrier_code }}）</dd>
          <dt>运单号</dt><dd>{{ item.tracking_no_masked }}</dd>
          <dt>发货时间</dt><dd>{{ dateTime(item.shipped_at) }}</dd>
          <dt>最后同步</dt><dd>{{ dateTime(item.last_synced_at) }}</dd>
        </dl>
        <button v-if="auth.has('shipments:refresh') && item.shipment_status !== 'voided'" type="button" :disabled="busy" @click="refreshTracking">刷新物流</button>
      </article>

      <article class="card">
        <h2>商品明细</h2>
        <p v-for="line in item.items" :key="line.order_item_id">
          {{ line.product_name }} · {{ line.sku_name }} × {{ line.quantity }}
        </p>
      </article>
    </div>

    <article class="card">
      <h2>物流轨迹</h2>
      <ol v-if="item.latest_tracks.length" class="timeline">
        <li v-for="track in item.latest_tracks" :key="`${track.occurred_at}-${track.track_status}`">
          <strong>{{ track.track_status }}</strong>
          <p>{{ track.description }}<span v-if="track.location_text"> · {{ track.location_text }}</span></p>
          <time>{{ dateTime(track.occurred_at) }}</time>
        </li>
      </ol>
      <p v-else>暂无物流轨迹。</p>
    </article>

    <div v-if="canCorrect || canVoid" class="settings-grid">
      <form v-if="canCorrect" class="card" @submit.prevent="correctTracking">
        <h2>更正运单号</h2>
        <p>仅未揽收、尚无物流轨迹的包裹允许更正；新运单号不会在页面完整回显。</p>
        <label>新运单号<input v-model="trackingNo" required minlength="6" maxlength="64" autocomplete="off" /></label>
        <label>原因码<input v-model="correctionReasonCode" required pattern="[A-Z][A-Z0-9_]+" /></label>
        <label>更正原因<textarea v-model="correctionReason" required minlength="2" maxlength="500" /></label>
        <button :disabled="busy || !trackingNo.trim() || !correctionReason.trim()">确认更正</button>
      </form>

      <form v-if="canVoid" class="card" @submit.prevent="voidShipment">
        <h2>作废包裹</h2>
        <p>作废不可撤销；已揽收、已有轨迹或被售后流程占用时，服务端将拒绝操作。</p>
        <label>原因码<input v-model="voidReasonCode" required pattern="[A-Z][A-Z0-9_]+" /></label>
        <label>作废原因<textarea v-model="voidReason" required minlength="2" maxlength="500" /></label>
        <button class="danger" :disabled="busy || !voidReason.trim()">确认作废</button>
      </form>
    </div>
  </section>
  <p v-else-if="!error">正在加载…</p>
  <p v-else class="alert error" role="alert">{{ error }}</p>
</template>
