<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import {
  getProduct,
  getProductReviews,
  getProductSkus,
  type ProductDetailData,
  type ProductReview,
  type ProductReviewList,
  type ProductSku,
  type ReviewImage,
} from '@/api/catalog'
import { errorMessage, resolveApiAssetUrl, type PaginationMeta } from '@/api/http'
import PageState from '@/components/PageState.vue'

const route = useRoute()
const router = useRouter()
const product = ref<ProductDetailData | null>(null)
const skus = ref<ProductSku[]>([])
const reviews = ref<ProductReviewList | null>(null)
const pagination = ref<PaginationMeta | null>(null)
const rating = ref('')
const hasImage = ref('')
const skuId = ref('')
const sort = ref('newest')
const loading = ref(true)
const error = ref('')
const previewImages = ref<ReviewImage[]>([])
const previewIndex = ref(0)
const previewDialog = ref<HTMLDialogElement | null>(null)
const currentPreview = computed(() => previewImages.value[previewIndex.value] ?? null)
let requestVersion = 0

function one(value: unknown): string { return typeof value === 'string' ? value : '' }
function productId(): string { return String(route.params.productId) }

function syncFilters() {
  rating.value = one(route.query.rating)
  hasImage.value = one(route.query.has_image)
  skuId.value = one(route.query.sku_id)
  sort.value = one(route.query.sort) || 'newest'
}

async function load() {
  const version = ++requestVersion
  loading.value = true
  error.value = ''
  try {
    const [productResponse, skuResponse, reviewResponse] = await Promise.all([
      getProduct(productId()),
      getProductSkus(productId()),
      getProductReviews(productId(), {
        rating: one(route.query.rating) || undefined,
        has_image: one(route.query.has_image) || undefined,
        sku_id: one(route.query.sku_id) || undefined,
        sort: one(route.query.sort) || 'newest',
        cursor: one(route.query.cursor) || undefined,
      }),
    ])
    if (version !== requestVersion) return
    product.value = productResponse.data
    skus.value = skuResponse.data.items
    reviews.value = reviewResponse.data
    pagination.value = reviewResponse.meta.pagination
    document.title = `${product.value.product_name} - 商品评价`
  } catch (cause) {
    if (version === requestVersion) error.value = errorMessage(cause)
  } finally {
    if (version === requestVersion) loading.value = false
  }
}

function applyFilters() {
  void router.push({ path: route.path, query: Object.fromEntries(Object.entries({
    rating: rating.value,
    has_image: hasImage.value,
    sku_id: skuId.value,
    sort: sort.value === 'newest' ? '' : sort.value,
  }).filter(([, value]) => value)) })
}

function changeCursor(cursor: string | null | undefined) {
  if (cursor) void router.push({ path: route.path, query: { ...route.query, cursor }, hash: '#review-list' })
}

function stars(value: number): string { return `${'★'.repeat(value)}${'☆'.repeat(5 - value)}` }

function openPreview(images: ReviewImage[], index: number) {
  previewImages.value = images
  previewIndex.value = index
  previewDialog.value?.showModal()
}

function movePreview(delta: number) {
  const length = previewImages.value.length
  if (length) previewIndex.value = (previewIndex.value + delta + length) % length
}

function date(value: string): string { return new Date(value).toLocaleDateString('zh-CN') }

onMounted(() => { syncFilters(); void load() })
watch(() => route.fullPath, () => { syncFilters(); void load() })
</script>

<template>
  <PageState :loading="loading" :error="error" @retry="load">
    <section v-if="product && reviews" class="storefront-stack narrow-reviews">
      <header class="page-heading">
        <div><p class="eyebrow">真实购买反馈</p><h1>{{ product.product_name }} · 全部评价</h1></div>
        <RouterLink :to="`/products/${product.product_id}`">返回商品详情</RouterLink>
      </header>

      <section class="review-summary" aria-label="评价概览">
        <div><strong>{{ reviews.summary.average_rating }}</strong><span>{{ stars(Math.round(Number(reviews.summary.average_rating))) }}</span><small>共 {{ reviews.summary.review_count }} 条公开评价</small></div>
        <dl><div v-for="score in [5, 4, 3, 2, 1]" :key="score"><dt>{{ score }} 星</dt><dd>{{ reviews.summary.rating_distribution[String(score)] || 0 }}</dd></div><div><dt>有图评价</dt><dd>{{ reviews.summary.image_review_count }}</dd></div></dl>
      </section>

      <form class="review-filters" @submit.prevent="applyFilters">
        <label>评分<select v-model="rating"><option value="">全部评分</option><option v-for="score in [5, 4, 3, 2, 1]" :key="score" :value="score">{{ score }} 星</option></select></label>
        <label>图片<select v-model="hasImage"><option value="">全部评价</option><option value="true">仅看有图</option><option value="false">仅看无图</option></select></label>
        <label>规格<select v-model="skuId"><option value="">全部规格</option><option v-for="sku in skus" :key="sku.sku_id" :value="sku.sku_id">{{ sku.sku_name }}</option></select></label>
        <label>排序<select v-model="sort"><option value="newest">最新优先</option><option value="oldest">最早优先</option></select></label>
        <button type="submit">应用筛选</button>
      </form>

      <div id="review-list" class="review-list" tabindex="-1">
        <PageState :empty="reviews.items.length === 0" empty-title="当前筛选下暂无评价" empty-detail="可以切换评分、规格或图片条件。">
          <article v-for="review in reviews.items" :key="review.review_id" class="review-card">
            <header><strong>{{ review.user_display_name }}</strong><span class="review-stars" :aria-label="`${review.rating} 星`">{{ stars(review.rating) }}</span><time :datetime="review.published_at">{{ date(review.published_at) }}</time></header>
            <small>购买规格：{{ review.sku_name }}</small>
            <p v-if="review.content" class="review-content">{{ review.content }}</p>
            <div v-if="review.images.length" class="review-images">
              <button v-for="(image, index) in review.images" :key="image.file_id" type="button" @click="openPreview(review.images, index)"><img :src="resolveApiAssetUrl(image.thumbnail_url) || undefined" alt="查看评价图片大图" width="96" height="96" loading="lazy" /></button>
            </div>
            <aside v-if="review.append" class="review-append"><strong>追评（{{ date(review.append.published_at) }}）</strong><p>{{ review.append.content }}</p></aside>
            <aside v-if="review.merchant_reply" class="merchant-reply"><strong>商家回复</strong><p>{{ review.merchant_reply.content }}</p></aside>
          </article>
          <nav v-if="pagination" class="pagination" aria-label="评价分页"><button type="button" class="secondary" :disabled="!pagination.has_previous" @click="changeCursor(pagination.previous_cursor)">上一页</button><span>每页 {{ pagination.limit }} 条</span><button type="button" class="secondary" :disabled="!pagination.has_next" @click="changeCursor(pagination.next_cursor)">下一页</button></nav>
        </PageState>
      </div>

      <dialog ref="previewDialog" class="image-dialog" @click.self="previewDialog?.close()">
        <button type="button" class="dialog-close" aria-label="关闭图片预览" @click="previewDialog?.close()">关闭</button>
        <div v-if="currentPreview" class="dialog-image-wrap"><button type="button" class="secondary" aria-label="上一张" @click="movePreview(-1)">←</button><img :src="resolveApiAssetUrl(currentPreview.url) || undefined" alt="评价图片大图" /><button type="button" class="secondary" aria-label="下一张" @click="movePreview(1)">→</button></div>
        <p>{{ previewIndex + 1 }} / {{ previewImages.length }}</p>
      </dialog>
    </section>
  </PageState>
</template>
