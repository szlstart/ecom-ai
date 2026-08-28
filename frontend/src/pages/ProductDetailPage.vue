<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import areaData from 'china-area-data'

import {
  formatMoney,
  getProduct,
  getProductFaqs,
  getProductSkus,
  setProductFavorite,
  type ProductDetailData,
  type ProductFaq,
  type ProductSku,
  type PublicImage,
  type Money,
} from '@/api/catalog'
import { addCartItem } from '@/api/cart'
import { createBuyNowCheckout } from '@/api/checkout'
import { createIdempotencyKey, errorMessage, resolveApiAssetUrl } from '@/api/http'
import { ensureStoreConversation, setConversationContext } from '@/api/messaging'
import PageState from '@/components/PageState.vue'
import SafeContentRenderer from '@/components/SafeContentRenderer.vue'
import CheckoutPage from '@/pages/CheckoutPage.vue'
import { useUserAuthStore } from '@/stores/user-auth'
import { useMessageCenterStore } from '@/stores/message-center'

const route = useRoute()
const router = useRouter()
const auth = useUserAuthStore()
const messageCenter = useMessageCenterStore()
const product = ref<ProductDetailData | null>(null)
const skus = ref<ProductSku[]>([])
const faqs = ref<ProductFaq[]>([])
const selectedSkuId = ref('')
const selectedImageId = ref('')
const quantity = ref(1)
const loading = ref(true)
const error = ref('')
const favoriteBusy = ref(false)
const cartBusy = ref(false)
const cartNotice = ref('')
const pendingCartRequest = ref<{ signature: string; key: string } | null>(null)
const buyBusy = ref(false)
const buyCheckoutId = ref('')
const buyCheckoutSignature = ref('')
const buyCheckoutOpen = ref(false)
const checkoutDialog = ref<HTMLElement | null>(null)
const contactBusy = ref(false)

const selectedSku = computed(() => skus.value.find((item) => item.sku_id === selectedSkuId.value) ?? null)
const gallery = computed<PublicImage[]>(() => selectedSku.value?.images ?? [])
const selectedImage = computed(() => gallery.value.find((item) => item.file_id === selectedImageId.value) ?? gallery.value[0] ?? null)
const maxQuantity = computed(() => Math.max(1, selectedSku.value?.max_purchase_quantity ?? 1))
const canPurchase = computed(() => selectedSku.value?.stock_status === 'in_stock' || selectedSku.value?.stock_status === 'low_stock')
const paymentTotal = computed<Money>(() => {
  const unitPrice = selectedSku.value?.sale_price ?? product.value?.price_range[0] ?? { minor_units: '0', currency: 'CNY' }
  return {
    minor_units: (BigInt(unitPrice.minor_units) * BigInt(quantity.value)).toString(),
    currency: unitPrice.currency,
  }
})
const returnTo = computed(() => {
  const value = typeof route.query.return_to === 'string' ? route.query.return_to : ''
  return value.startsWith('/search') || value.startsWith('/stores/') || value === '/' ? value : '/'
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const productId = String(route.params.productId)
    const [detailResponse, skuResponse, faqResponse] = await Promise.all([
      getProduct(productId, auth.accessToken), getProductSkus(productId), getProductFaqs(productId),
    ])
    product.value = detailResponse.data
    skus.value = skuResponse.data.items
    faqs.value = faqResponse.data.items
    selectedSkuId.value = typeof route.query.sku_id === 'string' && skus.value.some((item) => item.sku_id === route.query.sku_id)
      ? route.query.sku_id
      : detailResponse.data.default_sku_id ?? skus.value.find((item) => item.sku_status === 'active')?.sku_id ?? ''
    selectedImageId.value = gallery.value[0]?.file_id ?? ''
    document.title = detailResponse.data.product_name
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

function selectSku(sku: ProductSku) {
  selectedSkuId.value = sku.sku_id
  selectedImageId.value = sku.images[0]?.file_id ?? ''
  quantity.value = 1
  void router.replace({ query: { ...route.query, sku_id: sku.sku_id } })
}

function setQuantity(value: number) {
  quantity.value = Math.min(maxQuantity.value, Math.max(1, Math.trunc(value || 1)))
}

async function contactStore() {
  if (!product.value) return
  if (!auth.accessToken) {
    await router.push({ path: route.path, query: { ...route.query, auth: 'login', redirect: route.fullPath } })
    return
  }
  contactBusy.value = true
  error.value = ''
  try {
    const conversation = (await ensureStoreConversation(product.value.store.store_id, auth.accessToken)).data
    await setConversationContext(conversation.conversation_id, conversation.version, 'product', product.value.product_id, null, auth.accessToken)
    messageCenter.show(conversation.conversation_id)
  } catch (cause) { error.value = errorMessage(cause) }
  finally { contactBusy.value = false }
}

async function toggleFavorite() {
  if (!product.value) return
  if (!auth.accessToken) {
    await router.push({ path: route.path, query: { ...route.query, auth: 'login', redirect: route.fullPath } })
    return
  }
  favoriteBusy.value = true
  try {
    const next = !product.value.is_favorited
    await setProductFavorite(product.value.product_id, next, auth.accessToken)
    product.value.is_favorited = next
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    favoriteBusy.value = false
  }
}

async function addToCart() {
  if (!selectedSku.value || !canPurchase.value) return
  if (!auth.accessToken) {
    await router.push({ path: route.path, query: { ...route.query, auth: 'login', redirect: route.fullPath } })
    return
  }
  cartBusy.value = true
  cartNotice.value = ''
  error.value = ''
  const signature = `${selectedSku.value.sku_id}:${quantity.value}`
  if (pendingCartRequest.value?.signature !== signature) {
    pendingCartRequest.value = { signature, key: createIdempotencyKey('cart-add') }
  }
  try {
    const response = await addCartItem(
      selectedSku.value.sku_id,
      quantity.value,
      auth.accessToken,
      pendingCartRequest.value.key,
    )
    pendingCartRequest.value = null
    cartNotice.value = `已加入购物车，当前共 ${response.data.cart_total_quantity} 件商品。`
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    cartBusy.value = false
  }
}

async function buyNow() {
  if (!selectedSku.value || !canPurchase.value) return
  if (!auth.accessToken) { await router.push({ path: route.path, query: { ...route.query, auth: 'login', redirect: route.fullPath } }); return }
  const signature = `${selectedSku.value.sku_id}:${quantity.value}`
  if (buyCheckoutId.value && buyCheckoutSignature.value === signature) {
    buyCheckoutOpen.value = true
    return
  }
  buyBusy.value = true; error.value = ''
  try {
    const response = await createBuyNowCheckout(selectedSku.value.sku_id, quantity.value, auth.accessToken)
    buyCheckoutId.value = response.data.checkout_id
    buyCheckoutSignature.value = signature
    buyCheckoutOpen.value = true
  }
  catch (cause) { error.value = errorMessage(cause) }
  finally { buyBusy.value = false }
}

function closeBuyCheckout() {
  buyCheckoutOpen.value = false
}

function estimateText(): string {
  const estimate = product.value?.dispatch_estimate
  if (!estimate || estimate.status !== 'available' || !estimate.min_at || !estimate.max_at) return '暂时无法提供可靠发货时间范围'
  const format = (value: string) => new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' }).format(new Date(value))
  return `预计 ${format(estimate.min_at)} 至 ${format(estimate.max_at)} 发货（仅供参考）`
}

function originText(regionCode: string | null): string {
  if (!regionCode) return ''
  const code = regionCode.replace(/^CN[_-]/, '')
  if (!/^\d{2}(?:\d{2})?(?:\d{2})?$/.test(code)) return regionCode
  const normalizedCode = code.length === 2 ? `${code}0000` : code.length === 4 ? `${code}00` : code
  const provinceCode = `${normalizedCode.slice(0, 2)}0000`
  const province = areaData['86']?.[provinceCode] ?? ''
  const city = normalizedCode.endsWith('0000') ? '' : areaData[provinceCode]?.[normalizedCode] ?? ''
  return [province, city && city !== province ? city : ''].filter(Boolean).join(' ') || regionCode
}

onMounted(load)
watch(() => route.params.productId, load)
watch(() => auth.accessToken, () => void load())
watch(buyCheckoutOpen, async (open) => {
  document.body.classList.toggle('modal-open', open)
  if (open) {
    await nextTick()
    checkoutDialog.value?.focus()
  }
})
onBeforeUnmount(() => document.body.classList.remove('modal-open'))
</script>

<template>
  <PageState :loading="loading" :error="error" @retry="load">
    <article v-if="product" class="product-detail-page">
      <RouterLink :to="returnTo" class="back-link">← 返回上一页</RouterLink>
      <div class="product-detail-layout">
        <main class="product-detail-main">
          <section class="gallery" aria-label="商品图片">
            <div class="gallery-stage">
              <img v-if="selectedImage" :src="resolveApiAssetUrl(selectedImage.url) || undefined" :alt="selectedImage.alt_text || product.product_name" />
              <span v-else class="image-placeholder">暂无商品图片</span>
            </div>
            <div v-if="gallery.length > 1" class="thumbnail-row">
              <button v-for="image in gallery" :key="image.file_id" type="button" :class="{ selected: image.file_id === selectedImage?.file_id }" @click="selectedImageId = image.file_id">
                <img :src="resolveApiAssetUrl(image.thumbnail_url) || undefined" :alt="image.alt_text || '商品缩略图'" width="72" height="72" />
              </button>
            </div>
          </section>

          <nav class="detail-tabs" aria-label="商品详情导航">
            <a href="#reviews">评价</a><a v-if="product.attributes.length" href="#specifications">规格参数</a><a href="#details">商品详情</a><a href="#faqs">常见问题</a>
          </nav>

          <section id="reviews" class="content-section">
            <div class="section-heading"><div><p class="eyebrow">真实反馈</p><h2>商品评价</h2></div><RouterLink :to="`/products/${product.product_id}/reviews`">全部评价 →</RouterLink></div>
            <div v-if="product.review_count" class="product-review-overview"><strong>{{ product.rating_score }}</strong><span>综合评分</span><p>已有 {{ product.review_count }} 条真实购买评价</p></div>
            <p v-else class="muted">暂无评价，购买并完成订单后可以分享使用感受。</p>
          </section>

          <section v-if="product.attributes.length" id="specifications" class="content-section">
            <p class="eyebrow">商品信息</p><h2>规格参数</h2>
            <dl class="attribute-list">
              <template v-for="(attribute, index) in product.attributes" :key="index">
                <dt>{{ String(attribute.name || attribute.attribute_name || '属性') }}</dt>
                <dd>{{ String(attribute.value || attribute.attribute_value || '—') }}</dd>
              </template>
            </dl>
          </section>

          <section id="details" class="content-section">
            <p class="eyebrow">商品展示</p><h2>商品详情</h2>
            <SafeContentRenderer v-if="product.detail_content" :content="product.detail_content" />
            <p v-else class="muted">商家暂未补充详细说明。</p>
          </section>

          <section id="faqs" class="content-section">
            <p class="eyebrow">购买前须知</p><h2>常见问题</h2>
            <details v-for="faq in faqs" :key="faq.faq_id"><summary>{{ faq.question }}</summary><p>{{ faq.answer_content.safe_text_fallback }}</p></details>
            <p v-if="!faqs.length" class="muted">暂无常见问题。</p>
          </section>
        </main>

        <aside class="purchase-panel">
          <RouterLink :to="`/stores/${product.store.store_id}`" class="store-summary">
            <img v-if="product.store.logo_url" :src="resolveApiAssetUrl(product.store.logo_url) || undefined" alt="" width="44" height="44" />
            <span><strong>{{ product.store.store_name }}</strong><small>店铺评分 {{ product.store.rating_score }} · 进店逛逛 →</small></span>
          </RouterLink>
          <div><p class="eyebrow">商品信息</p><h1>{{ product.product_name }}</h1></div>
          <div class="purchase-price-card"><span>到手价</span><p class="detail-price">{{ formatMoney(selectedSku?.sale_price ?? product.price_range[0]) }}</p><small>全场包邮</small></div>
          <p class="product-meta"><span>已售 {{ selectedSku?.sales_count ?? product.sales_count }}</span><span>{{ product.review_count }} 条评价</span><span>评分 {{ product.rating_score }}</span></p>

          <fieldset v-if="skus.length" class="sku-fieldset">
            <legend>选择款式</legend>
            <button v-for="sku in skus" :key="sku.sku_id" type="button" class="sku-option" :class="{ selected: sku.sku_id === selectedSkuId }" :aria-pressed="sku.sku_id === selectedSkuId" :disabled="sku.sku_status !== 'active'" @click="selectSku(sku)">
              <span class="sku-option-thumb"><img v-if="sku.images[0]" :src="resolveApiAssetUrl(sku.images[0].thumbnail_url) || undefined" :alt="`${sku.sku_name}缩略图`" /><b v-else>{{ sku.sku_name.slice(0, 1) }}</b></span><span class="sku-option-copy"><strong>{{ sku.sku_name }}</strong><small>{{ formatMoney(sku.sale_price) }} · {{ sku.stock_status === 'out_of_stock' ? '缺货' : '可选' }}</small></span>
            </button>
          </fieldset>

          <p :class="['stock-line', { warning: selectedSku?.stock_status === 'low_stock' }]">
            {{ selectedSku?.stock_status === 'out_of_stock' ? '当前款式暂时缺货' : selectedSku?.stock_status === 'low_stock' ? `库存紧张，最多可购 ${maxQuantity} 件` : '当前款式有货' }}
          </p>
          <p v-if="originText(product.origin_region_code)" class="muted">发货地：{{ originText(product.origin_region_code) }}</p>
          <p class="muted">{{ estimateText() }}</p>
          <label class="quantity-control">数量
            <span><button type="button" class="secondary" :disabled="quantity <= 1" @click="setQuantity(quantity - 1)">−</button><input :value="quantity" inputmode="numeric" aria-label="购买数量" @input="setQuantity(Number(($event.target as HTMLInputElement).value))" /><button type="button" class="secondary" :disabled="quantity >= maxQuantity" @click="setQuantity(quantity + 1)">＋</button></span>
          </label>
          <small v-if="quantity >= maxQuantity">已达本次可购买上限，结算时仍会重新校验。</small>
          <div class="purchase-total-card" aria-live="polite">
            <span><b>支付总额</b><small>{{ quantity }} 件商品 · 包邮</small></span>
            <strong>{{ formatMoney(paymentTotal) }}</strong>
          </div>
          <div class="purchase-benefits"><span>✓ 邮寄包邮</span><span>✓ 库存实时校验</span><span>✓ 支持售后申请</span></div>
          <div class="purchase-actions">
            <button type="button" class="secondary" :disabled="contactBusy" @click="contactStore">{{ contactBusy ? '进入客服…' : '联系客服' }}</button>
            <button type="button" class="secondary" :disabled="favoriteBusy" @click="toggleFavorite">{{ product.is_favorited ? '取消收藏' : '收藏商品' }}</button>
            <button type="button" :disabled="cartBusy || !canPurchase" :title="canPurchase ? '加入购物车' : '当前款式不可购买'" @click="addToCart">{{ cartBusy ? '加入中…' : '加入购物车' }}</button>
            <button type="button" :disabled="buyBusy || !canPurchase" @click="buyNow">{{ buyBusy ? '创建结算…' : '立即购买' }}</button>
          </div>
          <p v-if="cartNotice" class="notice success" aria-live="polite">{{ cartNotice }} <RouterLink to="/cart">查看购物车</RouterLink></p>
          <p v-if="product.purchase_notice" class="alert info">{{ product.purchase_notice }}</p>
        </aside>
      </div>
      <div class="mobile-purchase-bar" aria-label="移动端购买操作">
        <button type="button" class="secondary" :disabled="contactBusy" @click="contactStore">客服</button><button type="button" :disabled="cartBusy || !canPurchase" @click="addToCart">加入购物车</button><button type="button" :disabled="buyBusy || !canPurchase" @click="buyNow">立即购买</button>
      </div>
      <Teleport to="body">
        <div v-if="buyCheckoutOpen && buyCheckoutId" class="buy-now-checkout-overlay" @mousedown.self="closeBuyCheckout" @keydown.esc="closeBuyCheckout">
          <section ref="checkoutDialog" class="buy-now-checkout-dialog" role="dialog" aria-modal="true" aria-labelledby="buy-now-checkout-title" tabindex="-1">
            <header class="buy-now-checkout-dialog-header">
              <div><p class="eyebrow">无需离开商品页</p><h2 id="buy-now-checkout-title">确认本次购买</h2></div>
              <button type="button" class="buy-now-checkout-close secondary" aria-label="关闭结算弹窗并继续浏览商品" @click="closeBuyCheckout">×</button>
            </header>
            <CheckoutPage :checkout-id="buyCheckoutId" embedded />
          </section>
        </div>
      </Teleport>
    </article>
  </PageState>
</template>
