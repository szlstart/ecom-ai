<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, type RouteLocationRaw } from 'vue-router'

import type { ChatMessage } from '@/api/messaging'

const props = withDefaults(defineProps<{
  message: ChatMessage
  audience?: 'user' | 'merchant' | 'admin'
}>(), { audience: 'user' })
const emit = defineEmits<{ navigate: [] }>()

type JsonObject = Record<string, unknown>

function objectValue(value: unknown): JsonObject {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}
}
function stringValue(value: unknown): string { return typeof value === 'string' ? value : '' }
function integerValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) ? value : null
}
function safeImageUrl(value: unknown): string | null {
  const url = stringValue(value)
  return /^\/api\/v1\/files\/file_[0-9A-Z]+(?:\?variant=thumbnail)?$/.test(url) ? url : null
}
function money(value: unknown): string {
  const amount = objectValue(value)
  const minorUnits = Number(stringValue(amount.minor_units))
  const currency = stringValue(amount.currency) || 'CNY'
  if (!Number.isSafeInteger(minorUnits)) return '价格待确认'
  return new Intl.NumberFormat('zh-CN', { style: 'currency', currency }).format(minorUnits / 100)
}
function orderStatus(value: unknown): string {
  return ({
    pending_payment: '待付款', paid: '已付款', pending_shipment: '待发货', shipped: '运输中',
    completed: '已完成', cancelled: '已取消', closed: '已关闭',
  } as Record<string, string>)[stringValue(value)] ?? '状态更新中'
}
function productStatus(value: unknown): string {
  return ({ on_sale: '销售中', off_shelf: '已下架', sold_out: '已售罄' } as Record<string, string>)[stringValue(value)] ?? '状态更新中'
}

const content = computed(() => objectValue(props.message.content))
const store = computed(() => objectValue(content.value.store))
const productId = computed(() => stringValue(content.value.product_id))
const skuId = computed(() => stringValue(content.value.sku_id))
const orderId = computed(() => stringValue(content.value.order_id))
const productImage = computed(() => safeImageUrl(content.value.image_url))
const storeLogo = computed(() => safeImageUrl(store.value.logo_url))
const orderItems = computed(() => Array.isArray(content.value.items)
  ? content.value.items.filter((item): item is JsonObject => Boolean(item && typeof item === 'object')).slice(0, 2)
  : [])
const productRoute = computed<RouteLocationRaw>(() => {
  if (props.audience === 'merchant') return `/merchant/products/${encodeURIComponent(productId.value)}`
  if (props.audience === 'admin') return `/admin/products/${encodeURIComponent(productId.value)}`
  return { path: `/products/${productId.value}`, query: skuId.value ? { sku_id: skuId.value } : {} }
})
const orderRoute = computed<RouteLocationRaw>(() => {
  if (props.audience === 'merchant') return { path: '/merchant/orders', query: { order_id: orderId.value } }
  if (props.audience === 'admin') return `/admin/orders/${encodeURIComponent(orderId.value)}`
  return `/me/orders/${encodeURIComponent(orderId.value)}`
})
</script>

<template>
  <p v-if="message.text" class="chat-message-text">{{ message.text }}</p>

  <RouterLink v-else-if="message.message_type === 'product_card' && productId" class="rich-message-card product-message-card" :to="productRoute" @click="emit('navigate')">
    <div class="rich-card-cover">
      <img v-if="productImage" :src="productImage" :alt="stringValue(content.product_name)" loading="lazy" />
      <span v-else aria-hidden="true">商</span>
      <i :class="{ unavailable: content.stock_status !== 'available' }">{{ content.stock_status === 'available' ? '有货' : '暂时无货' }}</i>
    </div>
    <div class="rich-card-copy">
      <div class="rich-card-store">
        <img v-if="storeLogo" :src="storeLogo" alt="" loading="lazy" />
        <span v-else>{{ stringValue(store.store_name).slice(0, 1) || '店' }}</span>
        <small>{{ stringValue(store.store_name) || '店铺' }}</small>
      </div>
      <strong>{{ stringValue(content.product_name) || '商品' }}</strong>
      <p>{{ stringValue(content.sku_name) || '默认款式' }}</p>
      <div class="rich-card-meta"><b>{{ money(content.price) }}</b><span>已售 {{ integerValue(content.sales_count) ?? 0 }}</span><span>库存 {{ integerValue(content.available_quantity) ?? 0 }}</span></div>
    </div>
    <footer><span>{{ productStatus(content.product_status) }}</span><strong>查看商品 ›</strong></footer>
  </RouterLink>

  <RouterLink v-else-if="message.message_type === 'order_card' && orderId" class="rich-message-card order-message-card" :to="orderRoute" @click="emit('navigate')">
    <header>
      <span class="rich-card-logo"><img v-if="storeLogo" :src="storeLogo" alt="" loading="lazy" /><i v-else>{{ stringValue(store.store_name).slice(0, 1) || '店' }}</i></span>
      <div><strong>{{ stringValue(store.store_name) || '店铺订单' }}</strong><small>订单 {{ stringValue(content.display_order_id) || orderId }}</small></div>
      <b>{{ orderStatus(content.order_status) }}</b>
    </header>
    <div class="order-card-items">
      <article v-for="(item, index) in orderItems" :key="`${stringValue(item.sku_id)}-${index}`">
        <span><img v-if="safeImageUrl(item.image_url)" :src="safeImageUrl(item.image_url)!" :alt="stringValue(item.product_name)" loading="lazy" /><i v-else>物</i></span>
        <div><strong>{{ stringValue(item.product_name) || '订单商品' }}</strong><small>{{ stringValue(item.sku_name) || '默认款式' }} · ×{{ integerValue(item.quantity) ?? 1 }}</small></div>
      </article>
    </div>
    <footer><span>共 {{ integerValue(content.total_quantity) ?? 0 }} 件</span><b>实付 {{ money(content.payable_amount) }}</b><strong>查看订单 ›</strong></footer>
  </RouterLink>

  <p v-else-if="!message.text" class="chat-message-fallback">{{ message.message_type === 'system' ? '系统状态已更新' : '暂不支持展示这类消息' }}</p>
</template>

<style scoped>
.chat-message-text,.chat-message-fallback{margin:0;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere}.chat-message-fallback{color:#687287}
.rich-message-card{width:min(410px,62vw);overflow:hidden;display:grid;color:#1d2735;border:1px solid #e3e7ed;border-radius:14px;background:#fff;box-shadow:0 8px 22px rgb(27 49 101 / 8%);transition:transform .18s ease,box-shadow .18s ease}.rich-message-card:hover{transform:translateY(-1px);text-decoration:none;box-shadow:0 12px 28px rgb(27 49 101 / 13%)}
.product-message-card{grid-template-columns:112px minmax(0,1fr)}.rich-card-cover{position:relative;min-height:126px;display:grid;place-items:center;overflow:hidden;background:linear-gradient(145deg,#eef2f8,#dfe6f0)}.rich-card-cover>img{width:100%;height:100%;object-fit:cover}.rich-card-cover>span{font-size:2rem;font-weight:850;color:#8996a8}.rich-card-cover>i{position:absolute;left:8px;bottom:8px;padding:3px 7px;color:#fff;border-radius:999px;background:#278c63;font-size:.61rem;font-style:normal;font-weight:750}.rich-card-cover>i.unavailable{background:#737c89}.rich-card-copy{padding:11px 12px;min-width:0;display:grid;align-content:start;gap:5px}.rich-card-store{display:flex;align-items:center;gap:5px;color:#6f7885}.rich-card-store img,.rich-card-store>span{width:18px;height:18px;display:grid;place-items:center;border-radius:5px;background:#e6ebf3;object-fit:cover;font-size:.58rem;font-weight:800}.rich-card-store small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.rich-card-copy>strong{overflow:hidden;font-size:.92rem;line-height:1.4;text-overflow:ellipsis;white-space:nowrap}.rich-card-copy>p{margin:0;overflow:hidden;color:#687287;font-size:.72rem;text-overflow:ellipsis;white-space:nowrap}.rich-card-meta{margin-top:3px;display:flex;align-items:baseline;gap:8px}.rich-card-meta b{color:#d83931;font-size:1.02rem}.rich-card-meta span{color:#87909e;font-size:.62rem}.product-message-card>footer{grid-column:1/-1;padding:8px 11px;display:flex;justify-content:space-between;border-top:1px solid #eef0f4;color:#737d8c;font-size:.68rem}.product-message-card>footer strong{color:#3158d8}
.order-message-card{padding:13px;gap:11px}.order-message-card>header{display:grid;grid-template-columns:34px minmax(0,1fr) auto;align-items:center;gap:8px}.rich-card-logo,.rich-card-logo img,.rich-card-logo i{width:34px;height:34px}.rich-card-logo img,.rich-card-logo i{display:grid;place-items:center;border-radius:9px;background:#e9edf4;object-fit:cover;font-size:.7rem;font-style:normal;font-weight:800}.order-message-card>header>div{min-width:0;display:grid;gap:2px}.order-message-card>header>div strong,.order-message-card>header>div small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.order-message-card>header>div small{color:#89919e;font-size:.62rem}.order-message-card>header>b{color:#287f5f;font-size:.72rem}.order-card-items{padding:9px;display:grid;gap:7px;border-radius:10px;background:#f6f7f9}.order-card-items article{display:grid;grid-template-columns:48px minmax(0,1fr);align-items:center;gap:9px}.order-card-items article>span,.order-card-items article img,.order-card-items article i{width:48px;height:48px}.order-card-items article img,.order-card-items article i{display:grid;place-items:center;border-radius:8px;background:#e7ebf1;object-fit:cover;font-size:.68rem;font-style:normal}.order-card-items article>div{min-width:0;display:grid;gap:4px}.order-card-items article strong,.order-card-items article small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.order-card-items article strong{font-size:.76rem}.order-card-items article small{color:#7c8490;font-size:.64rem}.order-message-card>footer{display:flex;align-items:baseline;justify-content:flex-end;gap:9px;color:#7c8490;font-size:.66rem}.order-message-card>footer>b{color:#d83931;font-size:.88rem}.order-message-card>footer>strong{margin-left:auto;color:#3158d8}
@media(max-width:720px){.rich-message-card{width:min(100%,320px)}.product-message-card{grid-template-columns:88px minmax(0,1fr)}.rich-card-cover{min-height:116px}.rich-card-meta span:nth-last-child(1){display:none}}
</style>
