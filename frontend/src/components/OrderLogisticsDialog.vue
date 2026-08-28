<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

import { errorMessage } from '@/api/http'
import { getShipment, listOrderShipments, type ShipmentDetail, type ShipmentTrack } from '@/api/logistics'
import { useUserAuthStore } from '@/stores/user-auth'
import { chinaRegionName, formatChinaRegion } from '@/utils/china-regions'

const props = defineProps<{ orderId: string | null }>()
const emit = defineEmits<{ close: [] }>()
const auth = useUserAuthStore()
const dialog = ref<HTMLElement | null>(null)
const shipments = ref<ShipmentDetail[]>([])
const selectedId = ref('')
const loading = ref(false)
const error = ref('')
const copied = ref(false)
let pollTimer: number | undefined
let requestGeneration = 0

const current = computed(() => shipments.value.find((item) => item.shipment_id === selectedId.value) ?? shipments.value[0] ?? null)
const tracksNewestFirst = computed(() => [...(current.value?.latest_tracks ?? [])].reverse())
const currentStage = computed(() => {
  const status = current.value?.latest_tracks.at(-1)?.provider_status
  return ({ WAITING_PICKUP: 1, PICKED_UP: 2, OUT_FOR_DELIVERY: 3, DELIVERED: 4 } as Record<string, number>)[status ?? ''] ?? 0
})
const headline = computed(() => current.value?.latest_tracks.at(-1)?.description ?? '订单已支付，正在生成首条物流信息')
const currentLocation = computed(() => {
  const shipment = current.value
  const track = shipment?.latest_tracks.at(-1)
  if (!shipment || !track) return '商家正在打包，即将交给承运商'
  if (track.provider_status === 'DELIVERED') return `${formatChinaRegion(shipment.route)} ${shipment.route.destination_address}`
  if (track.provider_status === 'OUT_FOR_DELIVERY') return chinaRegionName(shipment.route.district_code)
  return locationName(track.location_text) || '物流节点更新中'
})
const routeOrigin = computed(() => locationName(current.value?.route.origin_region_code ?? null) || '商家发货地')
const routeDestination = computed(() => current.value ? formatChinaRegion(current.value.route) : '收货地')
const isTerminal = computed(() => shipments.value.length > 0 && shipments.value.every((item) => item.shipment_status === 'delivered'))

function locationName(value: string | null): string {
  if (!value) return ''
  return /^\d{6}$/.test(value) ? chinaRegionName(value) : value
}
function dateTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(value))
}
function close() { emit('close') }
function onKeydown(event: KeyboardEvent) { if (event.key === 'Escape') close() }
async function copyTracking() {
  if (!current.value) return
  await navigator.clipboard.writeText(current.value.tracking_no)
  copied.value = true
  window.setTimeout(() => { copied.value = false }, 1800)
}
async function load(showLoading = false) {
  if (!props.orderId || !auth.accessToken) return
  const generation = requestGeneration
  if (showLoading) loading.value = true
  error.value = ''
  try {
    const summaries = (await listOrderShipments(props.orderId, auth.accessToken)).data.items
    const details = await Promise.all(summaries.map(async (item) => (await getShipment(item.shipment_id, auth.accessToken!)).data))
    if (generation !== requestGeneration) return
    shipments.value = details
    if (!details.some((item) => item.shipment_id === selectedId.value)) selectedId.value = details[0]?.shipment_id ?? ''
    schedulePoll(details.length > 0 && details.every((item) => item.shipment_status === 'delivered') ? 0 : 1000)
  } catch (cause) {
    if (generation !== requestGeneration) return
    error.value = errorMessage(cause)
    schedulePoll(2000)
  } finally {
    if (generation === requestGeneration) loading.value = false
  }
}
function schedulePoll(delay: number) {
  if (pollTimer !== undefined) window.clearTimeout(pollTimer)
  pollTimer = delay > 0 ? window.setTimeout(() => { void load(false) }, delay) : undefined
}
function teardown() {
  requestGeneration += 1
  if (pollTimer !== undefined) window.clearTimeout(pollTimer)
  pollTimer = undefined
  document.body.classList.remove('modal-open')
  window.removeEventListener('keydown', onKeydown)
}

watch(() => props.orderId, async (orderId) => {
  teardown()
  shipments.value = []
  selectedId.value = ''
  error.value = ''
  if (!orderId) return
  document.body.classList.add('modal-open')
  window.addEventListener('keydown', onKeydown)
  const generation = requestGeneration
  await nextTick()
  if (generation === requestGeneration) dialog.value?.focus()
  await load(true)
}, { immediate: true })
onBeforeUnmount(teardown)
</script>

<template>
  <Teleport to="body">
    <div v-if="orderId" class="logistics-overlay" @mousedown.self="close">
      <section ref="dialog" class="logistics-dialog" role="dialog" aria-modal="true" aria-labelledby="logistics-title" tabindex="-1">
        <header class="logistics-dialog-header">
          <div><p>ORDER LOGISTICS</p><h2 id="logistics-title">查看物流</h2><span>订单号 {{ orderId }}</span></div>
          <button type="button" aria-label="关闭物流弹窗" @click="close">×</button>
        </header>

        <div v-if="error" class="logistics-error" role="alert"><strong>物流暂时没有加载成功</strong><span>{{ error }}</span><button type="button" @click="load(true)">重新加载</button></div>
        <div v-else-if="loading && !shipments.length" class="logistics-loading"><span class="logistics-loader" aria-hidden="true"></span><strong>正在查询物流信息</strong><p>支付后的模拟运单会由后端自动生成，无需手动刷新。</p></div>
        <div v-else-if="!shipments.length" class="logistics-loading"><span class="parcel-icon" aria-hidden="true">▣</span><strong>订单已支付，商家正在打包</strong><p>物流单生成后会自动出现在这里，请保持弹窗开启。</p></div>

        <template v-else-if="current">
          <nav v-if="shipments.length > 1" class="shipment-tabs" aria-label="选择包裹">
            <button v-for="(shipment, index) in shipments" :key="shipment.shipment_id" type="button" :class="{ active: shipment.shipment_id === current.shipment_id }" @click="selectedId = shipment.shipment_id">包裹 {{ index + 1 }}</button>
          </nav>

          <section class="logistics-hero">
            <div class="logistics-status-icon" :class="{ delivered: isTerminal }" aria-hidden="true">{{ isTerminal ? '✓' : '↗' }}</div>
            <div><span>{{ isTerminal ? '运输已完成' : '运输进行中 · 自动更新' }}</span><h3>{{ headline }}</h3><p>当前位置：{{ currentLocation }}</p></div>
          </section>

          <ol class="logistics-progress" aria-label="配送进度">
            <li v-for="(step, index) in ['待揽收', '运输中', '派送中', '已签收']" :key="step" :class="{ active: currentStage >= index + 1, current: currentStage === index + 1 }"><i>{{ currentStage > index + 1 ? '✓' : index + 1 }}</i><span>{{ step }}</span></li>
          </ol>

          <section class="route-card">
            <div><small>发货地</small><strong>{{ routeOrigin }}</strong></div><span class="route-line"><i></i></span><div><small>收货地</small><strong>{{ routeDestination }}</strong></div>
          </section>

          <div class="logistics-content">
            <section class="tracking-card">
              <header><div><small>承运商</small><strong>{{ current.carrier_name }}</strong></div><span class="simulation-badge">模拟物流</span></header>
              <dl><div><dt>物流编号</dt><dd>{{ current.tracking_no }} <button type="button" @click="copyTracking">{{ copied ? '已复制' : '复制' }}</button></dd></div><div><dt>包裹内容</dt><dd>{{ current.items.map((item) => `${item.product_name}（${item.sku_name}）×${item.quantity}`).join('、') }}</dd></div></dl>
            </section>

            <section class="tracks-card">
              <header><h3>物流轨迹</h3><span v-if="!isTerminal"><i></i>实时更新中</span></header>
              <ol v-if="tracksNewestFirst.length" class="logistics-timeline">
                <li v-for="(track, index) in tracksNewestFirst" :key="`${track.provider_status}-${track.occurred_at}`" :class="{ latest: index === 0 }">
                  <i></i><div><strong>{{ track.description }}</strong><p v-if="track.location_text">{{ locationName(track.location_text) }}</p><time :datetime="track.occurred_at">{{ dateTime(track.occurred_at) }}</time></div>
                </li>
              </ol>
              <div v-else class="waiting-track"><span></span><div><strong>等待第一条物流轨迹</strong><p>系统将在支付后约 5 秒更新为“已发货，待揽收”。</p></div></div>
            </section>
          </div>
          <footer class="logistics-note"><span>盾</span><p><strong>物流信息由后端持久化记录</strong><small>关闭弹窗、刷新页面或更换设备后，进度不会丢失。物流签收不等于确认收货。</small></p></footer>
        </template>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.logistics-overlay { position: fixed; z-index: 1200; inset: 0; padding: 22px; display: grid; place-items: center; background: rgb(12 18 32 / 68%); backdrop-filter: blur(5px); }
.logistics-dialog { width: min(880px, 100%); max-height: calc(100vh - 44px); overflow-y: auto; border: 1px solid #dbe2ef; border-radius: 24px; outline: 0; background: #f5f7fb; box-shadow: 0 32px 100px rgb(5 13 33 / 38%); }
.logistics-dialog-header { position: sticky; z-index: 4; top: 0; padding: 22px 26px; display: flex; justify-content: space-between; align-items: center; color: #fff; background: linear-gradient(125deg, #172b62, #3158d8 68%, #5578df); }
.logistics-dialog-header p, .logistics-dialog-header h2, .logistics-dialog-header span { margin: 0; }
.logistics-dialog-header p { color: #bfcdfa; font-size: .7rem; font-weight: 800; letter-spacing: .16em; }
.logistics-dialog-header h2 { margin: 3px 0; font-size: 1.45rem; }
.logistics-dialog-header span { color: #d9e2ff; font-size: .78rem; }
.logistics-dialog-header button { width: 38px; height: 38px; padding: 0; color: #fff; border: 1px solid rgb(255 255 255 / 28%); border-radius: 50%; background: rgb(255 255 255 / 10%); font-size: 1.5rem; }
.shipment-tabs { padding: 15px 24px 0; display: flex; gap: 8px; overflow-x: auto; }
.shipment-tabs button { flex: 0 0 auto; padding: 8px 14px; color: #53617e; border: 1px solid #d7deeb; background: #fff; }
.shipment-tabs button.active { color: #fff; border-color: #3158d8; background: #3158d8; }
.logistics-hero { padding: 25px 28px 20px; display: flex; gap: 16px; align-items: center; background: #fff; }
.logistics-status-icon { width: 54px; height: 54px; display: grid; flex: 0 0 auto; place-items: center; color: #fff; border-radius: 17px; background: linear-gradient(135deg, #3158d8, #6f8eed); font-size: 1.5rem; font-weight: 900; box-shadow: 0 10px 25px rgb(49 88 216 / 25%); }
.logistics-status-icon.delivered { background: linear-gradient(135deg, #20835a, #45ae7d); }
.logistics-hero span { color: #3158d8; font-size: .74rem; font-weight: 800; letter-spacing: .04em; }
.logistics-hero h3 { margin: 4px 0; font-size: 1.38rem; }
.logistics-hero p { margin: 0; color: #657188; }
.logistics-progress { margin: 0; padding: 18px 32px 24px; display: grid; grid-template-columns: repeat(4, 1fr); list-style: none; background: #fff; }
.logistics-progress li { position: relative; display: grid; justify-items: center; gap: 7px; color: #98a1b3; font-size: .78rem; }
.logistics-progress li::before { position: absolute; z-index: 0; top: 14px; left: -50%; width: 100%; height: 3px; content: ''; background: #e4e8f0; }
.logistics-progress li:first-child::before { display: none; }
.logistics-progress i { position: relative; z-index: 1; width: 29px; height: 29px; display: grid; place-items: center; border: 3px solid #e4e8f0; border-radius: 50%; background: #fff; font-style: normal; font-weight: 800; }
.logistics-progress li.active { color: #2449bc; font-weight: 800; }
.logistics-progress li.active::before { background: #5275dd; }
.logistics-progress li.active i { color: #fff; border-color: #3158d8; background: #3158d8; }
.logistics-progress li.current i { box-shadow: 0 0 0 6px rgb(49 88 216 / 12%); }
.route-card { margin: 18px 22px; padding: 17px 20px; display: grid; grid-template-columns: minmax(0, 1fr) 120px minmax(0, 1fr); align-items: center; gap: 16px; border: 1px solid #e0e5ee; border-radius: 16px; background: #fff; }
.route-card div { display: grid; gap: 4px; }
.route-card div:last-child { text-align: right; }
.route-card small { color: #8a94a7; }
.route-card strong { overflow-wrap: anywhere; }
.route-line { position: relative; height: 2px; background: #b8c5e8; }
.route-line::after { position: absolute; top: -4px; right: -1px; content: ''; border-width: 5px 0 5px 8px; border-style: solid; border-color: transparent transparent transparent #5275dd; }
.route-line i { position: absolute; top: -4px; left: 46%; width: 10px; height: 10px; border-radius: 50%; background: #3158d8; box-shadow: 0 0 0 4px #e7ecff; }
.logistics-content { padding: 0 22px; display: grid; grid-template-columns: minmax(250px, .78fr) minmax(0, 1.4fr); align-items: start; gap: 16px; }
.tracking-card, .tracks-card { padding: 20px; border: 1px solid #e0e5ee; border-radius: 16px; background: #fff; }
.tracking-card > header, .tracks-card > header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.tracking-card > header > div { display: grid; gap: 3px; }
.tracking-card small { color: #8a94a7; }
.simulation-badge { padding: 5px 8px; color: #805719; border-radius: 999px; background: #fff3d8; font-size: .68rem; font-weight: 800; }
.tracking-card dl { margin: 18px 0 0; display: grid; gap: 15px; }
.tracking-card dl div { display: grid; gap: 5px; }
.tracking-card dt { color: #8a94a7; font-size: .72rem; }
.tracking-card dd { margin: 0; overflow-wrap: anywhere; line-height: 1.55; }
.tracking-card dd button { padding: 3px 7px; color: #3158d8; border: 0; background: transparent; font-size: .72rem; }
.tracks-card h3 { margin: 0; }
.tracks-card > header span { display: flex; align-items: center; gap: 6px; color: #218158; font-size: .72rem; }
.tracks-card > header span i { width: 7px; height: 7px; border-radius: 50%; background: #27a36c; animation: logistics-pulse 1.3s infinite; }
.logistics-timeline { margin: 20px 0 0; padding: 0; list-style: none; }
.logistics-timeline li { position: relative; min-height: 80px; padding: 0 0 22px 28px; color: #7e889b; }
.logistics-timeline li::before { position: absolute; top: 10px; bottom: -3px; left: 5px; width: 2px; content: ''; background: #e1e6ef; }
.logistics-timeline li:last-child::before { display: none; }
.logistics-timeline li > i { position: absolute; z-index: 1; top: 5px; left: 0; width: 12px; height: 12px; border: 3px solid #b8c1d0; border-radius: 50%; background: #fff; }
.logistics-timeline li.latest { color: #172033; }
.logistics-timeline li.latest > i { border-color: #3158d8; box-shadow: 0 0 0 5px #e9edff; }
.logistics-timeline div { display: grid; gap: 5px; }
.logistics-timeline p, .logistics-timeline time { margin: 0; font-size: .78rem; }
.waiting-track { margin-top: 20px; padding: 16px; display: flex; gap: 12px; border-radius: 12px; background: #f6f8fc; }
.waiting-track > span { width: 10px; height: 10px; margin-top: 5px; border-radius: 50%; background: #3158d8; animation: logistics-pulse 1.3s infinite; }
.waiting-track p { margin: 5px 0 0; color: #788398; font-size: .78rem; }
.logistics-note { margin: 18px 22px 22px; padding: 14px 17px; display: flex; gap: 12px; align-items: center; color: #53617e; border-radius: 14px; background: #eaf4ee; }
.logistics-note > span { width: 36px; height: 36px; display: grid; place-items: center; color: #fff; border-radius: 50%; background: #24845a; font-size: .65rem; font-weight: 900; }
.logistics-note p { margin: 0; display: grid; gap: 3px; }
.logistics-note small { color: #667568; }
.logistics-loading, .logistics-error { min-height: 420px; padding: 40px; display: grid; place-items: center; align-content: center; gap: 12px; text-align: center; }
.logistics-loading p, .logistics-error span { margin: 0; color: #6f798e; }
.logistics-loader { width: 42px; height: 42px; border: 4px solid #dfe5f3; border-top-color: #3158d8; border-radius: 50%; animation: logistics-spin .8s linear infinite; }
.parcel-icon { width: 58px; height: 58px; display: grid; place-items: center; color: #3158d8; border-radius: 18px; background: #e9edff; font-size: 1.6rem; }
@keyframes logistics-spin { to { transform: rotate(360deg); } }
@keyframes logistics-pulse { 50% { opacity: .35; transform: scale(.8); } }
@media (max-width: 720px) {
  .logistics-overlay { padding: 0; align-items: end; }
  .logistics-dialog { max-height: 92vh; border-radius: 22px 22px 0 0; }
  .logistics-dialog-header { padding: 18px 20px; }
  .logistics-content { grid-template-columns: 1fr; padding-inline: 14px; }
  .route-card { margin-inline: 14px; grid-template-columns: 1fr 58px 1fr; padding-inline: 14px; }
  .logistics-note { margin-inline: 14px; }
  .logistics-progress { padding-inline: 15px; }
  .logistics-progress span { font-size: .7rem; }
}
</style>
