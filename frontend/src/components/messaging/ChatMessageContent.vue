<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink, type RouteLocationRaw } from 'vue-router'

import { resolveApiAssetUrl } from '@/api/http'
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
  return /^\/api\/v1\/files\/file_[0-9A-Z]+(?:\?variant=thumbnail)?$/.test(url)
    ? resolveApiAssetUrl(url)
    : null
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
const productCards = computed<JsonObject[]>(() => {
  if (props.message.message_type === 'product_card' && stringValue(content.value.product_id)) return [content.value]
  return Array.isArray(content.value.product_cards)
    ? content.value.product_cards
      .filter((item): item is JsonObject => Boolean(item && typeof item === 'object') && Boolean(stringValue((item as JsonObject).product_id)))
      .slice(0, 5)
    : []
})
const orderCards = computed<JsonObject[]>(() => {
  if (props.message.message_type === 'order_card' && stringValue(content.value.order_id)) return [content.value]
  return Array.isArray(content.value.order_cards)
    ? content.value.order_cards
      .filter((item): item is JsonObject => Boolean(item && typeof item === 'object') && Boolean(stringValue((item as JsonObject).order_id)))
      .slice(0, 5)
    : []
})
const detailCards = computed<JsonObject[]>(() => Array.isArray(content.value.detail_cards)
  ? content.value.detail_cards
    .filter((item): item is JsonObject => Boolean(item && typeof item === 'object'))
    .slice(0, 5)
  : [])
function productStore(card: JsonObject): JsonObject { return objectValue(card.store) }
function productRoute(card: JsonObject): RouteLocationRaw {
  const productId = stringValue(card.product_id)
  const skuId = stringValue(card.sku_id)
  if (props.audience === 'merchant') return `/merchant/products/${encodeURIComponent(productId)}`
  if (props.audience === 'admin') return `/admin/products/${encodeURIComponent(productId)}`
  return { path: `/products/${productId}`, query: skuId ? { sku_id: skuId } : {} }
}
function orderRoute(card: JsonObject): RouteLocationRaw {
  const orderId = stringValue(card.order_id)
  if (props.audience === 'merchant') return { path: '/merchant/orders', query: { order_id: orderId } }
  if (props.audience === 'admin') return `/admin/orders/${encodeURIComponent(orderId)}`
  return `/me/orders/${encodeURIComponent(orderId)}`
}
function orderStore(card: JsonObject): JsonObject { return objectValue(card.store) }
function orderItems(card: JsonObject): JsonObject[] {
  return Array.isArray(card.items)
    ? card.items.filter((item): item is JsonObject => Boolean(item && typeof item === 'object')).slice(0, 2)
    : []
}
function detailRows(card: JsonObject): JsonObject[] {
  return Array.isArray(card.rows)
    ? card.rows.filter((item): item is JsonObject => Boolean(item && typeof item === 'object')).slice(0, 8)
    : []
}
function detailRoute(card: JsonObject): RouteLocationRaw | null {
  const action = objectValue(card.action)
  const resourceId = stringValue(action.resource_id)
  if (!resourceId) return null
  if (action.resource_type === 'product') return productRoute({ product_id: resourceId })
  if (action.resource_type === 'order') return orderRoute({ order_id: resourceId })
  if (action.resource_type === 'refund') return `/me/after-sales/${encodeURIComponent(resourceId)}`
  return null
}
</script>

<template>
  <p v-if="message.text" class="chat-message-text">{{ message.text }}</p>

  <div v-if="productCards.length" class="product-card-list">
    <RouterLink v-for="card in productCards" :key="stringValue(card.product_id)" class="rich-message-card product-message-card" :to="productRoute(card)" @click="emit('navigate')">
      <div class="rich-card-cover">
        <img v-if="safeImageUrl(card.image_url)" :src="safeImageUrl(card.image_url)!" :alt="stringValue(card.product_name)" loading="lazy" />
        <span v-else aria-hidden="true">商</span>
        <i :class="{ unavailable: card.stock_status !== 'available' }">{{ card.stock_status === 'available' ? '有货' : '暂时无货' }}</i>
      </div>
      <div class="rich-card-copy">
        <div class="rich-card-store">
          <img v-if="safeImageUrl(productStore(card).logo_url)" :src="safeImageUrl(productStore(card).logo_url)!" alt="" loading="lazy" />
          <span v-else>{{ stringValue(productStore(card).store_name).slice(0, 1) || '店' }}</span>
          <small>{{ stringValue(productStore(card).store_name) || '店铺' }}</small>
        </div>
        <strong>{{ stringValue(card.product_name) || '商品' }}</strong>
        <p>{{ stringValue(card.sku_name) || '默认款式' }}</p>
        <div class="rich-card-meta"><b>{{ money(card.price) }}</b><span>已售 {{ integerValue(card.sales_count) ?? 0 }}</span><span>库存 {{ integerValue(card.available_quantity) ?? 0 }}</span></div>
      </div>
      <footer><span>{{ productStatus(card.product_status) }}</span><strong>查看商品 ›</strong></footer>
    </RouterLink>
  </div>

  <div v-if="orderCards.length" class="order-card-list">
    <RouterLink v-for="card in orderCards" :key="stringValue(card.order_id)" class="rich-message-card order-message-card" :to="orderRoute(card)" @click="emit('navigate')">
      <header>
        <span class="rich-card-logo"><img v-if="safeImageUrl(orderStore(card).logo_url)" :src="safeImageUrl(orderStore(card).logo_url)!" alt="" loading="lazy" /><i v-else>{{ stringValue(orderStore(card).store_name).slice(0, 1) || '店' }}</i></span>
        <div><strong>{{ stringValue(orderStore(card).store_name) || '店铺订单' }}</strong><small>订单 {{ stringValue(card.display_order_id) || '详情' }}</small></div>
        <b>{{ orderStatus(card.order_status) }}</b>
      </header>
      <div class="order-card-items">
        <article v-for="(item, index) in orderItems(card)" :key="`${stringValue(item.sku_id)}-${index}`">
          <span><img v-if="safeImageUrl(item.image_url)" :src="safeImageUrl(item.image_url)!" :alt="stringValue(item.product_name)" loading="lazy" /><i v-else>物</i></span>
          <div><strong>{{ stringValue(item.product_name) || '订单商品' }}</strong><small>{{ stringValue(item.sku_name) || '默认款式' }} · ×{{ integerValue(item.quantity) ?? 1 }}</small></div>
        </article>
      </div>
      <footer><span>共 {{ integerValue(card.total_quantity) ?? 0 }} 件</span><b>实付 {{ money(card.payable_amount) }}</b><strong>查看订单 ›</strong></footer>
    </RouterLink>
  </div>

  <div v-if="detailCards.length" class="detail-card-list">
    <section v-for="(card, cardIndex) in detailCards" :key="`${stringValue(card.kind)}-${cardIndex}`" class="rich-message-card detail-message-card">
      <header>
        <span>{{ stringValue(card.icon) || '✓' }}</span>
        <div><small>{{ stringValue(card.eyebrow) }}</small><strong>{{ stringValue(card.title) || '查询结果' }}</strong></div>
        <b :class="stringValue(card.tone)">{{ stringValue(card.badge) }}</b>
      </header>
      <p v-if="stringValue(card.summary)">{{ stringValue(card.summary) }}</p>
      <div v-if="detailRows(card).length" class="detail-card-rows">
        <article v-for="(row, rowIndex) in detailRows(card)" :key="`${stringValue(row.label)}-${rowIndex}`">
          <div><strong>{{ stringValue(row.label) }}</strong><small v-if="stringValue(row.meta)">{{ stringValue(row.meta) }}</small></div>
          <b>{{ stringValue(row.value) }}</b>
        </article>
      </div>
      <RouterLink v-if="detailRoute(card)" :to="detailRoute(card)!" @click="emit('navigate')">{{ stringValue(objectValue(card.action).label) || '查看详情' }} ›</RouterLink>
    </section>
  </div>

  <p v-if="!message.text && !productCards.length && !orderCards.length && !detailCards.length" class="chat-message-fallback">{{ message.message_type === 'system' ? '系统状态已更新' : '暂不支持展示这类消息' }}</p>
</template>

<style scoped>
.chat-message-text,.chat-message-fallback{margin:0;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere}.chat-message-fallback{color:#687287}
.product-card-list,.order-card-list,.detail-card-list{display:grid;gap:10px}.chat-message-text+.product-card-list,.chat-message-text+.order-card-list,.chat-message-text+.detail-card-list{margin-top:9px}
.product-card-list:has(>.product-message-card:nth-child(2)){width:min(620px,64vw);grid-template-columns:repeat(2,minmax(0,1fr))}.product-card-list:has(>.product-message-card:nth-child(2)) .product-message-card{width:100%}
.rich-message-card{width:min(410px,62vw);overflow:hidden;display:grid;color:#1d2735;border:1px solid #e3e7ed;border-radius:14px;background:#fff;box-shadow:0 8px 22px rgb(27 49 101 / 8%);transition:transform .18s ease,box-shadow .18s ease}.rich-message-card:hover{transform:translateY(-1px);text-decoration:none;box-shadow:0 12px 28px rgb(27 49 101 / 13%)}
.product-message-card{grid-template-columns:112px minmax(0,1fr)}.rich-card-cover{position:relative;min-height:126px;display:grid;place-items:center;overflow:hidden;background:linear-gradient(145deg,#eef2f8,#dfe6f0)}.rich-card-cover>img{width:100%;height:100%;object-fit:cover}.rich-card-cover>span{font-size:2rem;font-weight:850;color:#8996a8}.rich-card-cover>i{position:absolute;left:8px;bottom:8px;padding:3px 7px;color:#fff;border-radius:999px;background:#278c63;font-size:.61rem;font-style:normal;font-weight:750}.rich-card-cover>i.unavailable{background:#737c89}.rich-card-copy{padding:11px 12px;min-width:0;display:grid;align-content:start;gap:5px}.rich-card-store{display:flex;align-items:center;gap:5px;color:#6f7885}.rich-card-store img,.rich-card-store>span{width:18px;height:18px;display:grid;place-items:center;border-radius:5px;background:#e6ebf3;object-fit:cover;font-size:.58rem;font-weight:800}.rich-card-store small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.rich-card-copy>strong{overflow:hidden;font-size:.92rem;line-height:1.4;text-overflow:ellipsis;white-space:nowrap}.rich-card-copy>p{margin:0;overflow:hidden;color:#687287;font-size:.72rem;text-overflow:ellipsis;white-space:nowrap}.rich-card-meta{margin-top:3px;display:flex;align-items:baseline;gap:8px}.rich-card-meta b{color:#d83931;font-size:1.02rem}.rich-card-meta span{color:#87909e;font-size:.62rem}.product-message-card>footer{grid-column:1/-1;padding:8px 11px;display:flex;justify-content:space-between;border-top:1px solid #eef0f4;color:#737d8c;font-size:.68rem}.product-message-card>footer strong{color:#3158d8}
.order-message-card{padding:13px;gap:11px}.order-message-card>header{display:grid;grid-template-columns:34px minmax(0,1fr) auto;align-items:center;gap:8px}.rich-card-logo,.rich-card-logo img,.rich-card-logo i{width:34px;height:34px}.rich-card-logo img,.rich-card-logo i{display:grid;place-items:center;border-radius:9px;background:#e9edf4;object-fit:cover;font-size:.7rem;font-style:normal;font-weight:800}.order-message-card>header>div{min-width:0;display:grid;gap:2px}.order-message-card>header>div strong,.order-message-card>header>div small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.order-message-card>header>div small{color:#89919e;font-size:.62rem}.order-message-card>header>b{color:#287f5f;font-size:.72rem}.order-card-items{padding:9px;display:grid;gap:7px;border-radius:10px;background:#f6f7f9}.order-card-items article{display:grid;grid-template-columns:48px minmax(0,1fr);align-items:center;gap:9px}.order-card-items article>span,.order-card-items article img,.order-card-items article i{width:48px;height:48px}.order-card-items article img,.order-card-items article i{display:grid;place-items:center;border-radius:8px;background:#e7ebf1;object-fit:cover;font-size:.68rem;font-style:normal}.order-card-items article>div{min-width:0;display:grid;gap:4px}.order-card-items article strong,.order-card-items article small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.order-card-items article strong{font-size:.76rem}.order-card-items article small{color:#7c8490;font-size:.64rem}.order-message-card>footer{display:flex;align-items:baseline;justify-content:flex-end;gap:9px;color:#7c8490;font-size:.66rem}.order-message-card>footer>b{color:#d83931;font-size:.88rem}.order-message-card>footer>strong{margin-left:auto;color:#3158d8}
.detail-message-card{padding:14px;gap:11px}.detail-message-card>header{display:grid;grid-template-columns:34px minmax(0,1fr) auto;align-items:center;gap:9px}.detail-message-card>header>span{width:34px;height:34px;display:grid;place-items:center;border-radius:10px;color:#fff;background:linear-gradient(145deg,#345fe3,#6f91f4);font-size:.86rem;font-weight:850}.detail-message-card>header>div{min-width:0;display:grid;gap:2px}.detail-message-card>header small{color:#7c8593;font-size:.61rem;letter-spacing:.08em}.detail-message-card>header strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.88rem}.detail-message-card>header>b{padding:4px 8px;border-radius:999px;color:#256c53;background:#e7f6ef;font-size:.62rem}.detail-message-card>header>b.warning{color:#98601b;background:#fff2d9}.detail-message-card>header>b.danger{color:#ad3939;background:#fdeaea}.detail-message-card>p{margin:0;color:#626d7e;font-size:.71rem;line-height:1.55}.detail-card-rows{display:grid;overflow:hidden;border:1px solid #edf0f4;border-radius:10px;background:#f8f9fb}.detail-card-rows article{padding:9px 10px;display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:12px}.detail-card-rows article+article{border-top:1px solid #e9edf2}.detail-card-rows article>div{min-width:0;display:grid;gap:3px}.detail-card-rows article strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.72rem}.detail-card-rows article small{overflow:hidden;color:#7b8594;font-size:.62rem;text-overflow:ellipsis;white-space:nowrap}.detail-card-rows article>b{color:#d83931;font-size:.72rem}.detail-message-card>a{justify-self:end;color:#3158d8;font-size:.68rem;font-weight:800}.detail-message-card>a:hover{text-decoration:none;color:#173da9}
@media(max-width:720px){.rich-message-card{width:min(100%,320px)}.product-card-list:has(>.product-message-card:nth-child(2)){width:min(100%,320px);grid-template-columns:1fr}.product-message-card{grid-template-columns:88px minmax(0,1fr)}.rich-card-cover{min-height:116px}.rich-card-meta span:nth-last-child(1){display:none}}
</style>
