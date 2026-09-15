<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink, type RouteLocationRaw } from 'vue-router'

import { resolveApiAssetUrl } from '@/api/http'
import type { ChatMessage } from '@/api/messaging'
import { formatChinaRegion } from '@/utils/china-regions'

const props = withDefaults(defineProps<{
  message: ChatMessage
  audience?: 'user' | 'merchant' | 'admin'
  approvalDecision?: (
    approvalId: string,
    decision: 'approve' | 'reject',
    version: number,
  ) => Promise<void>
}>(), { audience: 'user' })
const emit = defineEmits<{ navigate: []; prompt: [value: string] }>()

type JsonObject = Record<string, unknown>
type PreviewKind = 'product' | 'order' | 'detail' | 'cart' | 'address'
type CardPreview = { kind: PreviewKind; card: JsonObject; route: RouteLocationRaw | null }

const preview = ref<CardPreview | null>(null)
const approvalState = ref<'idle' | 'busy' | 'approved' | 'rejected' | 'error'>('idle')
const approvalError = ref('')
const previewDialog = ref<HTMLElement | null>(null)
let previewScrollState: { element: HTMLElement; top: number } | null = null

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
function orderStatus(value: unknown, afterSale: unknown, hasPendingReview: unknown): string {
  if (stringValue(afterSale) === 'in_progress') return '售后中'
  if (stringValue(afterSale) === 'partial') return '部分售后'
  if (stringValue(value) === 'completed' && hasPendingReview === true) return '待评价'
  return ({
    pending_payment: '待付款', paid: '已付款', pending_shipment: '待发货', shipped: '运输中',
    completed: '已完成', cancelled: '已取消', closed: '已关闭',
  } as Record<string, string>)[stringValue(value)] ?? '状态更新中'
}
function productStatus(value: unknown): string {
  return ({ on_sale: '销售中', off_shelf: '已下架', sold_out: '已售罄' } as Record<string, string>)[stringValue(value)] ?? '状态更新中'
}
function orderCreatedAt(value: unknown): string {
  const raw = stringValue(value)
  if (!raw) return '订单详情'
  const date = new Date(raw)
  if (Number.isNaN(date.getTime())) return '订单详情'
  return `下单于 ${new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date)}`
}

const content = computed(() => objectValue(props.message.content))
const agentAssetCard = computed<JsonObject | null>(() => (
  props.message.message_type === 'agent_asset' && stringValue(content.value.image_url)
    ? content.value
    : null
))
function normalizedProductCards(value: unknown, limit = 8): JsonObject[] {
  return Array.isArray(value)
    ? value
      .filter((item): item is JsonObject => Boolean(item && typeof item === 'object') && Boolean(stringValue((item as JsonObject).product_id)))
      .slice(0, limit)
    : []
}
const productCards = computed<JsonObject[]>(() => {
  if (props.message.message_type === 'product_card' && stringValue(content.value.product_id)) return [content.value]
  return normalizedProductCards(content.value.product_cards, 12)
})
const productCardGroups = computed<JsonObject[]>(() => Array.isArray(content.value.product_card_groups)
  ? content.value.product_card_groups
    .filter((item): item is JsonObject => Boolean(item && typeof item === 'object'))
    .map((item) => ({ ...item, cards: normalizedProductCards(item.cards, 6) }))
    .slice(0, 4)
  : [])
const orderCards = computed<JsonObject[]>(() => {
  if (props.message.message_type === 'order_card' && stringValue(content.value.order_id)) return [content.value]
  return Array.isArray(content.value.order_cards)
    ? content.value.order_cards
      .filter((item): item is JsonObject => Boolean(item && typeof item === 'object') && Boolean(stringValue((item as JsonObject).order_id)))
      .slice(0, 8)
    : []
})
const detailCards = computed<JsonObject[]>(() => Array.isArray(content.value.detail_cards)
  ? content.value.detail_cards
    .filter((item): item is JsonObject => Boolean(item && typeof item === 'object'))
    .slice(0, 12)
  : [])
const cartCard = computed<JsonObject | null>(() => {
  const value = objectValue(content.value.cart_card)
  return Object.keys(value).length ? value : null
})
const addressCards = computed<JsonObject[]>(() => Array.isArray(content.value.address_cards)
  ? content.value.address_cards
    .filter((item): item is JsonObject => Boolean(item && typeof item === 'object') && Boolean(stringValue((item as JsonObject).address_id)))
    .slice(0, 8)
  : [])
const approvalCard = computed<JsonObject | null>(() => {
  if (props.message.message_type !== 'agent_action_approval') return null
  const approvalId = stringValue(content.value.approval_id)
  const approvalVersion = integerValue(content.value.approval_version)
  return approvalId && approvalVersion !== null ? content.value : null
})
watch(approvalCard, (card) => {
  const status = stringValue(card?.approval_status)
  approvalState.value = status === 'approved' || status === 'consumed'
    ? 'approved'
    : status === 'rejected'
      ? 'rejected'
      : 'idle'
  approvalError.value = ''
}, { immediate: true })
const approvalChanges = computed<JsonObject[]>(() => {
  const value = approvalCard.value?.changes
  return Array.isArray(value)
    ? value.filter((item): item is JsonObject => Boolean(item && typeof item === 'object')).slice(0, 8)
    : []
})
const approvalDisplayState = computed(() => {
  if (approvalState.value === 'busy') return { label: '提交中', detail: '正在提交你的决定…' }
  const execution = stringValue(approvalCard.value?.execution_status)
  if (execution === 'succeeded') return { label: '已执行', detail: '操作已经完成，结果见下方回读卡片。' }
  if (execution === 'failed') return { label: '未完成', detail: '执行条件已变化，本次没有覆盖业务数据。' }
  if (execution === 'expired') return { label: '已过期', detail: '确认已过期，请重新发起操作。' }
  if (approvalState.value === 'rejected' || execution === 'cancelled') return { label: '已取消', detail: '本次操作已取消，不会修改业务数据。' }
  if (approvalState.value === 'approved' || execution === 'queued') return { label: '已确认', detail: '操作已确认，Agent 正在执行并回读结果。' }
  return { label: '待确认', detail: '' }
})
async function submitApproval(decision: 'approve' | 'reject') {
  const card = approvalCard.value
  if (!card || !props.approvalDecision || approvalState.value !== 'idle') return
  const approvalId = stringValue(card.approval_id)
  const version = integerValue(card.approval_version)
  if (!approvalId || version === null) return
  approvalState.value = 'busy'
  approvalError.value = ''
  try {
    await props.approvalDecision(approvalId, decision, version)
    approvalState.value = decision === 'approve' ? 'approved' : 'rejected'
  } catch (cause) {
    approvalState.value = 'error'
    approvalError.value = cause instanceof Error ? cause.message : '确认失败，请稍后重试。'
  }
}
function addressRegion(card: JsonObject): string {
  return formatChinaRegion({
    province_code: stringValue(card.province_code),
    city_code: stringValue(card.city_code),
    district_code: stringValue(card.district_code),
  })
}
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
function previewOrderItems(card: JsonObject): JsonObject[] {
  return Array.isArray(card.items)
    ? card.items.filter((item): item is JsonObject => Boolean(item && typeof item === 'object')).slice(0, 12)
    : []
}
function detailRows(card: JsonObject): JsonObject[] {
  return Array.isArray(card.rows)
    ? card.rows.filter((item): item is JsonObject => Boolean(item && typeof item === 'object')).slice(0, 8)
    : []
}
function detailRoute(card: JsonObject): RouteLocationRaw | null {
  const action = objectValue(card.action)
  const path = stringValue(action.path)
  const audiencePathAllowed = props.audience === 'merchant'
    ? path.startsWith('/merchant/')
    : props.audience === 'admin'
      ? path === '/admin' || path.startsWith('/admin/')
      : path.startsWith('/me/') || path.startsWith('/checkout/')
  if (path && audiencePathAllowed && !path.includes('..') && !path.includes('?')) return path
  const resourceId = stringValue(action.resource_id)
  if (!resourceId) return null
  if (action.resource_type === 'product') return productRoute({ product_id: resourceId })
  if (action.resource_type === 'order') return orderRoute({ order_id: resourceId })
  if (action.resource_type === 'refund') return `/me/after-sales/${encodeURIComponent(resourceId)}`
  return null
}
function detailPrompt(card: JsonObject): string {
  return stringValue(objectValue(card.action).prompt).trim()
}
function cartGroups(card: JsonObject): JsonObject[] {
  return Array.isArray(card.groups)
    ? card.groups.filter((item): item is JsonObject => Boolean(item && typeof item === 'object')).slice(0, 4)
    : []
}
function cartItems(group: JsonObject): JsonObject[] {
  return Array.isArray(group.items)
    ? group.items.filter((item): item is JsonObject => Boolean(item && typeof item === 'object')).slice(0, 4)
    : []
}
function previewTitle(item: CardPreview): string {
  if (item.kind === 'product') return stringValue(item.card.product_name) || '商品详情'
  if (item.kind === 'order') return `${stringValue(orderStore(item.card).store_name) || '店铺'}订单`
  if (item.kind === 'detail') return stringValue(item.card.title) || '业务处理结果'
  if (item.kind === 'cart') return '我的购物车'
  return '收货地址'
}
function previewEyebrow(kind: PreviewKind): string {
  return ({ product: '商品卡片', order: '订单卡片', detail: '业务结果', cart: '购物车', address: '收货信息' })[kind]
}
function previewIcon(kind: PreviewKind): string {
  return ({ product: '商', order: '单', detail: '✓', cart: '购', address: '址' })[kind]
}
function openPreview(kind: PreviewKind, card: JsonObject, route: RouteLocationRaw | null, event?: Event) {
  const source = event?.currentTarget instanceof Element ? event.currentTarget : null
  const timeline = source?.closest<HTMLElement>('.message-timeline, .merchant-chat-timeline, .admin-chat-timeline') ?? null
  previewScrollState = timeline ? { element: timeline, top: timeline.scrollTop } : null
  preview.value = { kind, card, route }
  document.body.classList.add('modal-open')
  window.addEventListener('keydown', onPreviewKeydown)
  void nextTick(() => {
    previewDialog.value?.focus({ preventScroll: true })
    if (previewScrollState) previewScrollState.element.scrollTop = previewScrollState.top
  })
}
function closePreview() {
  preview.value = null
  document.body.classList.remove('modal-open')
  window.removeEventListener('keydown', onPreviewKeydown)
  if (previewScrollState) previewScrollState.element.scrollTop = previewScrollState.top
  previewScrollState = null
}
function openFullPage() {
  closePreview()
  emit('navigate')
}
function submitPrompt() {
  if (!preview.value) return
  const prompt = detailPrompt(preview.value.card)
  if (!prompt) return
  closePreview()
  emit('prompt', prompt)
}
function onPreviewKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') closePreview()
}
onBeforeUnmount(closePreview)
</script>

<template>
  <p v-if="message.text" class="chat-message-text">{{ message.text }}</p>

  <section v-if="agentAssetCard" class="agent-asset-message-card" aria-label="提交给 Agent 的图片操作">
    <img v-if="safeImageUrl(agentAssetCard.image_url)" :src="safeImageUrl(agentAssetCard.image_url)!" alt="待核对图片" loading="lazy" />
    <span v-else>图</span>
    <div>
      <small>{{ agentAssetCard.purpose === 'store_logo' ? '店铺 Logo' : agentAssetCard.purpose === 'user_avatar' ? '用户头像' : '款式展示图片' }}</small>
      <strong>{{ agentAssetCard.purpose === 'user_avatar' ? stringValue(agentAssetCard.username) || '目标用户' : stringValue(agentAssetCard.store_name) || '目标店铺' }}</strong>
      <p v-if="agentAssetCard.purpose === 'product_sku_image'">{{ stringValue(agentAssetCard.product_name) }} · {{ stringValue(agentAssetCard.sku_name) }}</p>
      <b>等待 Agent 核对并生成确认卡</b>
    </div>
  </section>

  <section v-if="approvalCard" class="operations-approval-card" aria-label="操作确认卡">
    <header>
      <span>审</span>
      <div>
        <small>执行前确认</small>
        <strong>{{ stringValue(approvalCard.title) || '请核对本次操作' }}</strong>
      </div>
      <b>{{ approvalDisplayState.label }}</b>
    </header>
    <p v-if="stringValue(approvalCard.target_label)">
      <strong>操作对象</strong>{{ stringValue(approvalCard.target_label) }}
    </p>
    <div class="operations-approval-changes">
      <article v-for="(change, index) in approvalChanges" :key="`${stringValue(change.label)}-${index}`">
        <span>{{ stringValue(change.label) }}</span>
        <strong>{{ stringValue(change.value) }}</strong>
      </article>
    </div>
    <small v-if="approvalError" class="operations-approval-error">{{ approvalError }}</small>
    <footer v-if="approvalDecision && !['approved', 'rejected'].includes(approvalState)">
      <button type="button" :disabled="approvalState === 'busy'" @click="submitApproval('reject')">取消操作</button>
      <button type="button" class="confirm" :disabled="approvalState === 'busy'" @click="submitApproval('approve')">
        {{ approvalState === 'busy' ? '正在提交…' : '确认执行' }}
      </button>
    </footer>
    <footer v-else-if="approvalDisplayState.detail" class="operations-approval-settled">
      {{ approvalDisplayState.detail }}
    </footer>
  </section>

  <div v-if="productCardGroups.length" class="product-card-groups">
    <section v-for="(group, groupIndex) in productCardGroups" :key="`${stringValue(group.title)}-${groupIndex}`" class="product-card-group">
      <header><span>{{ groupIndex + 1 }}</span><div><strong>{{ stringValue(group.title) || `第 ${groupIndex + 1} 组` }}</strong><small>{{ normalizedProductCards(group.cards).length ? `${normalizedProductCards(group.cards).length} 件可选商品` : '暂未找到匹配商品' }}</small></div></header>
      <div v-if="normalizedProductCards(group.cards).length" class="product-card-list card-result-grid" :class="{ 'is-multi': normalizedProductCards(group.cards).length > 1 }">
        <RouterLink v-for="card in normalizedProductCards(group.cards)" :key="stringValue(card.product_id)" :to="productRoute(card)" custom v-slot="{ href }">
          <a class="rich-message-card product-message-card" :href="href" @click.prevent.stop="openPreview('product', card, productRoute(card), $event)">
            <div class="rich-card-cover">
              <img v-if="safeImageUrl(card.image_url)" :src="safeImageUrl(card.image_url)!" :alt="stringValue(card.product_name)" loading="lazy" />
              <span v-else aria-hidden="true">商</span>
              <i :class="{ unavailable: card.stock_status !== 'available' }">{{ card.stock_status === 'available' ? '有货' : '暂时无货' }}</i>
            </div>
            <div class="rich-card-copy">
              <div class="rich-card-store"><img v-if="safeImageUrl(productStore(card).logo_url)" :src="safeImageUrl(productStore(card).logo_url)!" alt="" loading="lazy" /><span v-else>{{ stringValue(productStore(card).store_name).slice(0, 1) || '店' }}</span><small>{{ stringValue(productStore(card).store_name) || '店铺' }}</small></div>
              <strong>{{ stringValue(card.product_name) || '商品' }}</strong>
              <p>{{ stringValue(card.sku_name) || '默认款式' }}</p>
              <div class="rich-card-meta"><b>{{ money(card.price) }}</b><span>已售 {{ integerValue(card.sales_count) ?? 0 }}</span><span>库存 {{ integerValue(card.available_quantity) ?? 0 }}</span></div>
            </div>
            <footer><span>{{ productStatus(card.product_status) }}</span><strong>查看商品 ›</strong></footer>
          </a>
        </RouterLink>
      </div>
    </section>
  </div>

  <div v-if="!productCardGroups.length && productCards.length" class="product-card-list card-result-grid" :class="{ 'is-multi': productCards.length > 1 }">
    <RouterLink v-for="card in productCards" :key="stringValue(card.product_id)" :to="productRoute(card)" custom v-slot="{ href }">
      <a class="rich-message-card product-message-card" :href="href" @click.prevent.stop="openPreview('product', card, productRoute(card), $event)">
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
      </a>
    </RouterLink>
  </div>

  <div v-if="orderCards.length" class="order-card-list card-result-grid" :class="{ 'is-multi': orderCards.length > 1 }">
    <RouterLink v-for="card in orderCards" :key="stringValue(card.order_id)" :to="orderRoute(card)" custom v-slot="{ href }">
      <a class="rich-message-card order-message-card" :href="href" @click.prevent.stop="openPreview('order', card, orderRoute(card), $event)">
      <header>
        <span class="rich-card-logo"><img v-if="safeImageUrl(orderStore(card).logo_url)" :src="safeImageUrl(orderStore(card).logo_url)!" alt="" loading="lazy" /><i v-else>{{ stringValue(orderStore(card).store_name).slice(0, 1) || '店' }}</i></span>
        <div><strong>{{ stringValue(orderStore(card).store_name) || '店铺订单' }}</strong><small>{{ orderCreatedAt(card.created_at) }}</small></div>
        <b>{{ orderStatus(card.order_status, card.after_sale_status, card.has_pending_review) }}</b>
      </header>
      <div class="order-card-items">
        <article v-for="(item, index) in orderItems(card)" :key="`${stringValue(item.sku_id)}-${index}`">
          <span><img v-if="safeImageUrl(item.image_url)" :src="safeImageUrl(item.image_url)!" :alt="stringValue(item.product_name)" loading="lazy" /><i v-else>物</i></span>
          <div><strong>{{ stringValue(item.product_name) || '订单商品' }}</strong><small>{{ stringValue(item.sku_name) || '默认款式' }} · ×{{ integerValue(item.quantity) ?? 1 }}</small></div>
        </article>
      </div>
      <footer><span>共 {{ integerValue(card.total_quantity) ?? 0 }} 件</span><b>实付 {{ money(card.payable_amount) }}</b><strong>查看订单 ›</strong></footer>
      </a>
    </RouterLink>
  </div>

  <div v-if="detailCards.length" class="detail-card-list card-result-grid" :class="{ 'is-multi': detailCards.length > 1 }">
    <section v-for="(card, cardIndex) in detailCards" :key="`${stringValue(card.kind)}-${cardIndex}`" class="rich-message-card detail-message-card interactive-message-card" role="button" tabindex="0" @click.stop="openPreview('detail', card, detailRoute(card), $event)" @keydown.enter.prevent="openPreview('detail', card, detailRoute(card), $event)" @keydown.space.prevent="openPreview('detail', card, detailRoute(card), $event)">
      <img v-if="safeImageUrl(card.image_url)" class="detail-card-image" :src="safeImageUrl(card.image_url)!" :alt="stringValue(card.title)" loading="lazy" />
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
      <span v-if="detailRoute(card) || detailPrompt(card)" class="detail-card-preview-action">{{ stringValue(objectValue(card.action).label) || '查看详情' }} ›</span>
    </section>
  </div>

  <div v-if="cartCard" class="cart-card-list card-result-grid">
    <RouterLink to="/cart" custom v-slot="{ href }">
      <a class="rich-message-card cart-message-card" :href="href" @click.prevent.stop="openPreview('cart', cartCard, '/cart', $event)">
      <header>
        <span>购</span>
        <div><small>我的购物车</small><strong>共 {{ integerValue(cartCard.total_quantity) ?? 0 }} 件 · 已选 {{ integerValue(cartCard.selected_quantity) ?? 0 }} 件</strong></div>
        <b>{{ money(cartCard.selected_amount) }}</b>
      </header>
      <section v-for="(group, groupIndex) in cartGroups(cartCard)" :key="`${stringValue(group.store_id)}-${groupIndex}`">
        <div class="cart-store"><span>{{ stringValue(group.store_name) || '店铺' }}</span><small>{{ integerValue(group.selected_quantity) ?? 0 }} 件已选</small></div>
        <div class="cart-preview-items">
          <article v-for="(item, itemIndex) in cartItems(group)" :key="`${stringValue(item.product_id)}-${itemIndex}`">
            <img v-if="safeImageUrl(item.image_url)" :src="safeImageUrl(item.image_url)!" :alt="stringValue(item.product_name)" loading="lazy" />
            <i v-else>物</i>
            <div><strong>{{ stringValue(item.product_name) || '商品' }}</strong><small>{{ stringValue(item.sku_name) }} · ×{{ integerValue(item.quantity) ?? 1 }}</small></div>
            <b>{{ money(item.current_price) }}</b>
          </article>
        </div>
      </section>
      <footer><span>价格变化与库存以购物车实时结果为准</span><strong>查看购物车 ›</strong></footer>
      </a>
    </RouterLink>
  </div>

  <div v-if="addressCards.length" class="address-card-list card-result-grid" :class="{ 'is-multi': addressCards.length > 1 }">
    <RouterLink v-for="card in addressCards" :key="stringValue(card.address_id)" to="/me/addresses" custom v-slot="{ href }">
      <a class="rich-message-card address-message-card" :href="href" @click.prevent.stop="openPreview('address', card, '/me/addresses', $event)">
      <header><span>址</span><div><strong>{{ stringValue(card.recipient_name) || '收货人' }}</strong><small>{{ stringValue(card.phone) }}</small></div><b v-if="card.is_default">默认地址</b></header>
      <p>{{ addressRegion(card) }} {{ stringValue(card.address) }}</p>
      <footer><span>仅当前账号可见</span><strong>管理地址 ›</strong></footer>
      </a>
    </RouterLink>
  </div>

  <p v-if="!message.text && !agentAssetCard && !approvalCard && !productCardGroups.length && !productCards.length && !orderCards.length && !detailCards.length && !cartCard && !addressCards.length" class="chat-message-fallback">{{ message.message_type === 'system' ? '系统状态已更新' : '暂不支持展示这类消息' }}</p>

  <Teleport to="body">
    <div v-if="preview" class="message-card-preview-overlay" @mousedown.self="closePreview" @click.self="closePreview">
      <section ref="previewDialog" class="message-card-preview-dialog" role="dialog" aria-modal="true" :aria-label="previewTitle(preview)" tabindex="-1">
        <header class="message-card-preview-header">
          <span :class="`kind-${preview.kind}`">{{ previewIcon(preview.kind) }}</span>
          <div><small>{{ previewEyebrow(preview.kind) }}</small><h2>{{ previewTitle(preview) }}</h2></div>
          <button type="button" aria-label="关闭卡片预览" @click="closePreview">×</button>
        </header>

        <div class="message-card-preview-content">
          <section v-if="preview.kind === 'product'" class="preview-product-hero">
            <div class="preview-product-image"><img v-if="safeImageUrl(preview.card.image_url)" :src="safeImageUrl(preview.card.image_url)!" :alt="stringValue(preview.card.product_name)" /><span v-else>商</span></div>
            <div class="preview-product-copy">
              <p>{{ stringValue(productStore(preview.card).store_name) || '店铺商品' }}</p>
              <h3>{{ stringValue(preview.card.product_name) || '商品' }}</h3>
              <strong>{{ money(preview.card.price) }}</strong>
              <dl><div><dt>当前款式</dt><dd>{{ stringValue(preview.card.sku_name) || '默认款式' }}</dd></div><div><dt>库存</dt><dd>{{ integerValue(preview.card.available_quantity) ?? 0 }} 件</dd></div><div><dt>销量</dt><dd>{{ integerValue(preview.card.sales_count) ?? 0 }} 件</dd></div><div><dt>状态</dt><dd>{{ productStatus(preview.card.product_status) }}</dd></div></dl>
            </div>
          </section>

          <template v-else-if="preview.kind === 'order'">
            <section class="preview-order-summary"><div><small>{{ orderCreatedAt(preview.card.created_at) }}</small><strong>{{ orderStatus(preview.card.order_status, preview.card.after_sale_status, preview.card.has_pending_review) }}</strong></div><div><span>共 {{ integerValue(preview.card.total_quantity) ?? 0 }} 件</span><b>实付 {{ money(preview.card.payable_amount) }}</b></div></section>
            <div class="preview-order-items"><article v-for="(item, index) in previewOrderItems(preview.card)" :key="`${stringValue(item.sku_id)}-${index}`"><span><img v-if="safeImageUrl(item.image_url)" :src="safeImageUrl(item.image_url)!" :alt="stringValue(item.product_name)" /><i v-else>物</i></span><div><strong>{{ stringValue(item.product_name) || '订单商品' }}</strong><small>{{ stringValue(item.sku_name) || '默认款式' }}</small></div><b>×{{ integerValue(item.quantity) ?? 1 }}</b></article></div>
          </template>

          <template v-else-if="preview.kind === 'detail'">
            <img v-if="safeImageUrl(preview.card.image_url)" class="preview-detail-image" :src="safeImageUrl(preview.card.image_url)!" :alt="previewTitle(preview)" loading="lazy" />
            <p v-if="stringValue(preview.card.summary)" class="preview-detail-summary">{{ stringValue(preview.card.summary) }}</p>
            <dl class="preview-detail-rows"><div v-for="(row, index) in detailRows(preview.card)" :key="`${stringValue(row.label)}-${index}`"><dt><strong>{{ stringValue(row.label) }}</strong><small v-if="stringValue(row.meta)">{{ stringValue(row.meta) }}</small></dt><dd>{{ stringValue(row.value) }}</dd></div></dl>
          </template>

          <template v-else-if="preview.kind === 'cart'">
            <section class="preview-cart-summary"><span>共 {{ integerValue(preview.card.total_quantity) ?? 0 }} 件，已选 {{ integerValue(preview.card.selected_quantity) ?? 0 }} 件</span><strong>{{ money(preview.card.selected_amount) }}</strong></section>
            <div class="preview-cart-groups"><section v-for="(group, groupIndex) in cartGroups(preview.card)" :key="`${stringValue(group.store_id)}-${groupIndex}`"><header><strong>{{ stringValue(group.store_name) || '店铺' }}</strong><small>{{ integerValue(group.selected_quantity) ?? 0 }} 件已选</small></header><article v-for="(item, itemIndex) in cartItems(group)" :key="`${stringValue(item.product_id)}-${itemIndex}`"><span><img v-if="safeImageUrl(item.image_url)" :src="safeImageUrl(item.image_url)!" :alt="stringValue(item.product_name)" /><i v-else>物</i></span><div><strong>{{ stringValue(item.product_name) || '商品' }}</strong><small>{{ stringValue(item.sku_name) }} · ×{{ integerValue(item.quantity) ?? 1 }}</small></div><b>{{ money(item.current_price) }}</b></article></section></div>
          </template>

          <section v-else class="preview-address-card"><span>址</span><div><small>{{ preview.card.is_default ? '默认收货地址' : '收货地址' }}</small><h3>{{ stringValue(preview.card.recipient_name) || '收货人' }} · {{ stringValue(preview.card.phone) }}</h3><p>{{ addressRegion(preview.card) }} {{ stringValue(preview.card.address) }}</p></div></section>
        </div>

        <footer class="message-card-preview-footer">
          <button type="button" class="secondary" @click="closePreview">返回对话</button>
          <button v-if="preview.kind === 'detail' && detailPrompt(preview.card)" type="button" class="primary" @click="submitPrompt">{{ stringValue(objectValue(preview.card.action).label) || '只重试此项' }}</button>
          <RouterLink v-if="preview.route" :to="preview.route" @click="openFullPage">打开完整页面</RouterLink>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.chat-message-text,.chat-message-fallback{margin:0;line-height:1.65;white-space:pre-wrap;overflow-wrap:anywhere}.chat-message-fallback{color:#687287}
.agent-asset-message-card{width:min(480px,100%);padding:10px;display:grid;grid-template-columns:88px minmax(0,1fr);align-items:center;gap:12px;color:#253246;border:1px solid #dfe5ef;border-radius:15px;background:#fff;box-shadow:0 8px 22px rgb(27 49 101 / 8%)}.agent-asset-message-card>img,.agent-asset-message-card>span{width:88px;height:88px;display:grid;place-items:center;object-fit:cover;color:#fff;border-radius:11px;background:linear-gradient(145deg,#3158d8,#6f91f4);font-weight:900}.agent-asset-message-card>div{min-width:0;display:grid;gap:4px}.agent-asset-message-card small{color:#7c8797;font-size:.64rem}.agent-asset-message-card strong,.agent-asset-message-card p{margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.agent-asset-message-card strong{font-size:.84rem}.agent-asset-message-card p{color:#667286;font-size:.7rem}.agent-asset-message-card b{color:#3158d8;font-size:.66rem}
.card-result-grid{width:min(100%,1040px);display:grid;gap:12px}.chat-message-text+.card-result-grid{margin-top:12px}.card-result-grid+.card-result-grid{margin-top:18px;padding-top:18px;border-top:1px solid #e2e7ef}.card-result-grid.is-multi{grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr))}.card-result-grid.is-multi>.rich-message-card{width:100%}
.product-card-groups{width:min(100%,1040px);margin-top:12px;display:grid;gap:18px}.product-card-group{padding:14px;display:grid;gap:12px;border:1px solid #dfe6f1;border-radius:18px;background:linear-gradient(145deg,#fbfcff,#f4f7fb)}.product-card-group>header{display:flex;align-items:center;gap:10px}.product-card-group>header>span{width:30px;height:30px;display:grid;place-items:center;border-radius:10px;color:#fff;background:#3158d8;font-size:.72rem;font-weight:900}.product-card-group>header>div{display:grid;gap:2px}.product-card-group>header strong{color:#202b3b;font-size:.86rem}.product-card-group>header small{color:#7a8595;font-size:.64rem}.product-card-group .card-result-grid{width:100%}
.rich-message-card{width:min(520px,100%);overflow:hidden;display:grid;color:#1d2735;border:1px solid #e3e7ed;border-radius:14px;background:#fff;box-shadow:0 8px 22px rgb(27 49 101 / 8%);transition:transform .18s ease,box-shadow .18s ease}.rich-message-card:hover{transform:translateY(-1px);text-decoration:none;box-shadow:0 12px 28px rgb(27 49 101 / 13%)}
.interactive-message-card{cursor:pointer}.interactive-message-card:focus-visible{outline:3px solid rgb(49 88 216 / 28%);outline-offset:3px}
.product-message-card{grid-template-columns:112px minmax(0,1fr)}.rich-card-cover{position:relative;min-height:126px;display:grid;place-items:center;overflow:hidden;background:linear-gradient(145deg,#eef2f8,#dfe6f0)}.rich-card-cover>img{width:100%;height:100%;object-fit:cover}.rich-card-cover>span{font-size:2rem;font-weight:850;color:#8996a8}.rich-card-cover>i{position:absolute;left:8px;bottom:8px;padding:3px 7px;color:#fff;border-radius:999px;background:#278c63;font-size:.61rem;font-style:normal;font-weight:750}.rich-card-cover>i.unavailable{background:#737c89}.rich-card-copy{padding:11px 12px;min-width:0;display:grid;align-content:start;gap:5px}.rich-card-store{display:flex;align-items:center;gap:5px;color:#6f7885}.rich-card-store img,.rich-card-store>span{width:18px;height:18px;display:grid;place-items:center;border-radius:5px;background:#e6ebf3;object-fit:cover;font-size:.58rem;font-weight:800}.rich-card-store small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.rich-card-copy>strong{overflow:hidden;font-size:.92rem;line-height:1.4;text-overflow:ellipsis;white-space:nowrap}.rich-card-copy>p{margin:0;overflow:hidden;color:#687287;font-size:.72rem;text-overflow:ellipsis;white-space:nowrap}.rich-card-meta{margin-top:3px;display:flex;align-items:baseline;gap:8px}.rich-card-meta b{color:#d83931;font-size:1.02rem}.rich-card-meta span{color:#87909e;font-size:.62rem}.product-message-card>footer{grid-column:1/-1;padding:8px 11px;display:flex;justify-content:space-between;border-top:1px solid #eef0f4;color:#737d8c;font-size:.68rem}.product-message-card>footer strong{color:#3158d8}
.order-message-card{padding:13px;gap:11px}.order-message-card>header{display:grid;grid-template-columns:34px minmax(0,1fr) auto;align-items:center;gap:8px}.rich-card-logo,.rich-card-logo img,.rich-card-logo i{width:34px;height:34px}.rich-card-logo img,.rich-card-logo i{display:grid;place-items:center;border-radius:9px;background:#e9edf4;object-fit:cover;font-size:.7rem;font-style:normal;font-weight:800}.order-message-card>header>div{min-width:0;display:grid;gap:2px}.order-message-card>header>div strong,.order-message-card>header>div small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.order-message-card>header>div small{color:#89919e;font-size:.62rem}.order-message-card>header>b{color:#287f5f;font-size:.72rem}.order-card-items{padding:9px;display:grid;gap:7px;border-radius:10px;background:#f6f7f9}.order-card-items article{display:grid;grid-template-columns:48px minmax(0,1fr);align-items:center;gap:9px}.order-card-items article>span,.order-card-items article img,.order-card-items article i{width:48px;height:48px}.order-card-items article img,.order-card-items article i{display:grid;place-items:center;border-radius:8px;background:#e7ebf1;object-fit:cover;font-size:.68rem;font-style:normal}.order-card-items article>div{min-width:0;display:grid;gap:4px}.order-card-items article strong,.order-card-items article small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.order-card-items article strong{font-size:.76rem}.order-card-items article small{color:#7c8490;font-size:.64rem}.order-message-card>footer{display:flex;align-items:baseline;justify-content:flex-end;gap:9px;color:#7c8490;font-size:.66rem}.order-message-card>footer>b{color:#d83931;font-size:.88rem}.order-message-card>footer>strong{margin-left:auto;color:#3158d8}
.detail-message-card{padding:14px;gap:11px}.detail-message-card>header{display:grid;grid-template-columns:34px minmax(0,1fr) auto;align-items:center;gap:9px}.detail-message-card>header>span{width:34px;height:34px;display:grid;place-items:center;border-radius:10px;color:#fff;background:linear-gradient(145deg,#345fe3,#6f91f4);font-size:.86rem;font-weight:850}.detail-message-card>header>div{min-width:0;display:grid;gap:2px}.detail-message-card>header small{color:#7c8593;font-size:.61rem;letter-spacing:.08em}.detail-message-card>header strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.88rem}.detail-message-card>header>b{padding:4px 8px;border-radius:999px;color:#256c53;background:#e7f6ef;font-size:.62rem}.detail-message-card>header>b.warning{color:#98601b;background:#fff2d9}.detail-message-card>header>b.danger{color:#ad3939;background:#fdeaea}.detail-message-card>p{margin:0;color:#626d7e;font-size:.71rem;line-height:1.55}.detail-card-rows{display:grid;overflow:hidden;border:1px solid #edf0f4;border-radius:10px;background:#f8f9fb}.detail-card-rows article{padding:9px 10px;display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:12px}.detail-card-rows article+article{border-top:1px solid #e9edf2}.detail-card-rows article>div{min-width:0;display:grid;gap:3px}.detail-card-rows article strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.72rem}.detail-card-rows article small{overflow:hidden;color:#7b8594;font-size:.62rem;text-overflow:ellipsis;white-space:nowrap}.detail-card-rows article>b{color:#d83931;font-size:.72rem}.detail-card-preview-action{justify-self:end;color:#3158d8;font-size:.68rem;font-weight:800}
.cart-message-card{padding:14px;gap:11px}.cart-message-card>header{display:grid;grid-template-columns:36px minmax(0,1fr) auto;align-items:center;gap:10px}.cart-message-card>header>span{width:36px;height:36px;display:grid;place-items:center;border-radius:11px;color:#fff;background:linear-gradient(145deg,#ef8b24,#f6b244);font-weight:850}.cart-message-card>header>div{min-width:0;display:grid;gap:2px}.cart-message-card>header small{color:#818997;font-size:.62rem}.cart-message-card>header strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.84rem}.cart-message-card>header>b{color:#d83931;font-size:.92rem}.cart-message-card>section{display:grid;gap:7px}.cart-store{display:flex;justify-content:space-between;color:#4e5867;font-size:.68rem;font-weight:750}.cart-store small{color:#8a929f;font-weight:600}.cart-preview-items{display:grid;overflow:hidden;border-radius:10px;background:#f7f8fa}.cart-preview-items article{padding:8px;display:grid;grid-template-columns:42px minmax(0,1fr) auto;align-items:center;gap:8px}.cart-preview-items article+article{border-top:1px solid #eaedf1}.cart-preview-items img,.cart-preview-items i{width:42px;height:42px;display:grid;place-items:center;border-radius:8px;background:#e7ebf1;object-fit:cover;font-size:.65rem;font-style:normal}.cart-preview-items article>div{min-width:0;display:grid;gap:3px}.cart-preview-items article strong,.cart-preview-items article small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.cart-preview-items article strong{font-size:.7rem}.cart-preview-items article small{color:#808997;font-size:.61rem}.cart-preview-items article>b{color:#d83931;font-size:.69rem}.cart-message-card>footer{display:flex;justify-content:space-between;gap:10px;color:#8a929f;font-size:.62rem}.cart-message-card>footer strong{color:#3158d8}
.address-message-card{padding:14px;gap:11px}.address-message-card>header{display:grid;grid-template-columns:36px minmax(0,1fr) auto;align-items:center;gap:10px}.address-message-card>header>span{width:36px;height:36px;display:grid;place-items:center;border-radius:11px;color:#fff;background:linear-gradient(145deg,#2b996c,#54b88b);font-weight:850}.address-message-card>header>div{min-width:0;display:grid;gap:2px}.address-message-card>header strong{font-size:.84rem}.address-message-card>header small{color:#7a8492;font-size:.66rem}.address-message-card>header>b{padding:4px 8px;border-radius:999px;color:#246b52;background:#e6f6ee;font-size:.62rem}.address-message-card>p{margin:0;padding:10px;border-radius:10px;color:#424c5b;background:#f7f9fa;font-size:.72rem;line-height:1.55}.address-message-card>footer{display:flex;justify-content:space-between;color:#87909e;font-size:.62rem}.address-message-card>footer strong{color:#3158d8}
.operations-approval-card{margin-top:12px;width:min(560px,100%);overflow:hidden;border:1px solid #dfe5ef;border-radius:16px;background:#fff;box-shadow:0 12px 30px rgb(31 49 83 / 11%)}.operations-approval-card>header{padding:14px 15px;display:grid;grid-template-columns:38px minmax(0,1fr) auto;align-items:center;gap:10px;border-bottom:1px solid #edf0f4;background:linear-gradient(135deg,#f7f9ff,#fff)}.operations-approval-card>header>span{width:38px;height:38px;display:grid;place-items:center;color:#fff;border-radius:11px;background:linear-gradient(145deg,#3158d8,#6f8ff1);font-weight:900}.operations-approval-card>header>div{min-width:0;display:grid;gap:2px}.operations-approval-card>header small{color:#7c8798;font-size:.64rem}.operations-approval-card>header strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.9rem}.operations-approval-card>header>b{padding:4px 8px;color:#865911;border-radius:999px;background:#fff2d4;font-size:.64rem}.operations-approval-card>p{margin:0;padding:12px 15px;display:flex;gap:12px;color:#465266;font-size:.73rem}.operations-approval-card>p strong{color:#7a8495}.operations-approval-changes{margin:0 15px 13px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));overflow:hidden;border:1px solid #e8ebf0;border-radius:11px;background:#f8f9fb}.operations-approval-changes article{padding:10px 11px;display:grid;gap:3px}.operations-approval-changes article:nth-child(even){border-left:1px solid #e8ebf0}.operations-approval-changes span{color:#7d8795;font-size:.63rem}.operations-approval-changes strong{color:#273446;font-size:.76rem}.operations-approval-card>footer{padding:12px 15px;display:flex;justify-content:flex-end;gap:9px;border-top:1px solid #edf0f4}.operations-approval-card>footer button{min-height:36px;padding:7px 15px;border:1px solid #dce2eb;border-radius:9px;background:#fff;font-size:.75rem;font-weight:800}.operations-approval-card>footer button.confirm{color:#fff;border-color:#3158d8;background:#3158d8}.operations-approval-card>footer button:disabled{opacity:.55;cursor:wait}.operations-approval-error{margin:0 15px 12px;display:block;color:#b33c36}.operations-approval-card>footer.operations-approval-settled{justify-content:flex-start;color:#2c765a;font-size:.7rem;font-weight:750}
.message-card-preview-overlay{position:fixed;z-index:1500;inset:0;padding:24px;display:grid;place-items:center;background:rgb(10 17 30 / 62%);backdrop-filter:blur(7px)}.message-card-preview-dialog{width:min(980px,calc(100vw - 36px));max-height:min(820px,calc(100vh - 40px));overflow:hidden;display:grid;grid-template-rows:auto minmax(0,1fr) auto;border:1px solid rgb(255 255 255 / 70%);border-radius:24px;outline:0;background:#f7f9fc;box-shadow:0 34px 110px rgb(5 12 28 / 42%)}.message-card-preview-header{padding:18px 22px;display:grid;grid-template-columns:44px minmax(0,1fr) 38px;align-items:center;gap:12px;border-bottom:1px solid #e2e7ef;background:#fff}.message-card-preview-header>span{width:44px;height:44px;display:grid;place-items:center;color:#fff;border-radius:14px;background:linear-gradient(145deg,#345fe3,#6f91f4);font-weight:900}.message-card-preview-header>span.kind-cart{background:linear-gradient(145deg,#ef8b24,#f6b244)}.message-card-preview-header>span.kind-address{background:linear-gradient(145deg,#2b996c,#54b88b)}.message-card-preview-header>div{min-width:0;display:grid;gap:3px}.message-card-preview-header small{color:#7e8999;font-size:.68rem;font-weight:800;letter-spacing:.08em}.message-card-preview-header h2{margin:0;overflow:hidden;color:#202b3b;font-size:1.12rem;text-overflow:ellipsis;white-space:nowrap}.message-card-preview-header>button{width:38px;height:38px;padding:0;color:#596575;border:1px solid #dce2eb;border-radius:11px;background:#fff;font-size:1.4rem}.message-card-preview-content{min-height:260px;padding:24px;overflow-y:auto}.message-card-preview-footer{padding:14px 22px;display:flex;justify-content:flex-end;gap:10px;border-top:1px solid #e2e7ef;background:#fff}.message-card-preview-footer>a,.message-card-preview-footer>.primary{min-height:38px;padding:8px 16px;display:inline-flex;align-items:center;justify-content:center;color:#fff;border:0;border-radius:8px;background:#3158d8;font-size:.82rem;font-weight:800}.message-card-preview-footer>a:hover,.message-card-preview-footer>.primary:hover{text-decoration:none;background:#2347bd}
.preview-product-hero{display:grid;grid-template-columns:minmax(260px,38%) minmax(0,1fr);gap:28px}.preview-product-image{min-height:330px;display:grid;place-items:center;overflow:hidden;border-radius:20px;background:linear-gradient(145deg,#edf1f7,#dfe6ef)}.preview-product-image img{width:100%;height:100%;object-fit:cover}.preview-product-image>span{color:#8794a6;font-size:4rem;font-weight:900}.preview-product-copy{padding:10px 0;display:grid;align-content:start;gap:13px}.preview-product-copy>p,.preview-product-copy>h3{margin:0}.preview-product-copy>p{color:#718095;font-size:.78rem}.preview-product-copy>h3{font-size:1.35rem;line-height:1.45}.preview-product-copy>strong{color:#d93e35;font-size:1.7rem}.preview-product-copy dl{margin:6px 0 0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.preview-product-copy dl>div{padding:13px;display:grid;gap:5px;border:1px solid #e5e9f0;border-radius:12px;background:#fff}.preview-product-copy dt{color:#8390a2;font-size:.68rem}.preview-product-copy dd{margin:0;color:#283548;font-size:.83rem;font-weight:750}
.preview-order-summary,.preview-cart-summary{padding:17px 19px;display:flex;align-items:center;justify-content:space-between;gap:16px;border:1px solid #e2e7ef;border-radius:15px;background:#fff}.preview-order-summary>div{display:grid;gap:4px}.preview-order-summary small{color:#8490a1}.preview-order-summary>div:last-child{text-align:right}.preview-order-summary b,.preview-cart-summary strong{color:#d93e35;font-size:1.18rem}.preview-order-items{margin-top:15px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.preview-order-items article,.preview-cart-groups article{padding:11px;display:grid;grid-template-columns:58px minmax(0,1fr) auto;align-items:center;gap:11px;border:1px solid #e5e9ef;border-radius:13px;background:#fff}.preview-order-items article>span,.preview-order-items img,.preview-order-items i,.preview-cart-groups article>span,.preview-cart-groups article img,.preview-cart-groups article i{width:58px;height:58px}.preview-order-items img,.preview-order-items i,.preview-cart-groups article img,.preview-cart-groups article i{display:grid;place-items:center;border-radius:10px;background:#edf1f5;object-fit:cover;font-style:normal}.preview-order-items article>div,.preview-cart-groups article>div{min-width:0;display:grid;gap:4px}.preview-order-items article strong,.preview-order-items article small,.preview-cart-groups article strong,.preview-cart-groups article small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.preview-order-items article small,.preview-cart-groups article small{color:#7d8899;font-size:.72rem}
.preview-detail-summary{margin:0 0 15px;padding:16px 18px;color:#526074;border-radius:14px;background:#edf2ff;line-height:1.65}.preview-detail-rows{margin:0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.preview-detail-rows>div{padding:14px 16px;display:grid;grid-template-columns:minmax(88px,max-content) minmax(0,1fr);align-items:center;gap:14px;border:1px solid #e3e8ef;border-radius:13px;background:#fff}.preview-detail-rows dt{min-width:0;display:grid;gap:4px;white-space:nowrap;word-break:keep-all;overflow-wrap:normal}.preview-detail-rows dt small{overflow:hidden;color:#818c9d;font-size:.68rem;font-weight:500;text-overflow:ellipsis}.preview-detail-rows dd{min-width:0;margin:0;color:#cf3f36;font-size:.82rem;font-weight:800;text-align:right;overflow-wrap:anywhere}
.preview-cart-summary{margin-bottom:15px}.preview-cart-groups{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.preview-cart-groups>section{padding:14px;display:grid;align-content:start;gap:9px;border:1px solid #e2e7ef;border-radius:15px;background:#fff}.preview-cart-groups>section>header{display:flex;justify-content:space-between}.preview-cart-groups>section>header small{color:#7d8899}.preview-cart-groups article{padding:8px;border:0;background:#f6f8fa}.preview-cart-groups article>span,.preview-cart-groups article img,.preview-cart-groups article i{width:48px;height:48px}.preview-cart-groups article>b{color:#d93e35}
.preview-address-card{min-height:280px;padding:34px;display:grid;grid-template-columns:72px minmax(0,1fr);align-items:center;gap:24px;border:1px solid #dce7e1;border-radius:20px;background:linear-gradient(145deg,#fff,#f1faf6)}.preview-address-card>span{width:72px;height:72px;display:grid;place-items:center;color:#fff;border-radius:22px;background:linear-gradient(145deg,#2b996c,#54b88b);font-size:1.3rem;font-weight:900}.preview-address-card>div{display:grid;gap:9px}.preview-address-card small{color:#27815f;font-weight:800}.preview-address-card h3,.preview-address-card p{margin:0}.preview-address-card h3{font-size:1.2rem}.preview-address-card p{color:#4d5c55;font-size:1rem;line-height:1.7}
@media(max-width:720px){.card-result-grid,.rich-message-card{width:100%}.card-result-grid.is-multi{grid-template-columns:1fr}.product-message-card{grid-template-columns:88px minmax(0,1fr)}.rich-card-cover{min-height:116px}.rich-card-meta span:nth-last-child(1){display:none}.message-card-preview-overlay{padding:10px}.message-card-preview-dialog{width:100%;max-height:calc(100vh - 20px);border-radius:18px}.message-card-preview-content{padding:16px}.preview-product-hero,.preview-order-items,.preview-detail-rows,.preview-cart-groups{grid-template-columns:1fr}.preview-product-image{min-height:230px}.preview-address-card{min-height:220px;padding:22px;grid-template-columns:52px minmax(0,1fr)}.preview-address-card>span{width:52px;height:52px;border-radius:16px}}
.detail-card-image{width:100%;height:150px;object-fit:cover;border-radius:11px;background:#edf1f6}
.preview-detail-image{display:block;width:100%;max-height:420px;object-fit:contain;border-radius:16px;background:#f3f6fb;border:1px solid #e3e8f1}
</style>
