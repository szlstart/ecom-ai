<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { errorMessage, resolveApiAssetUrl } from '@/api/http'
import { getMyOrder } from '@/api/orders'
import { createReview, getReviewEligibility, type ReviewEligibility } from '@/api/reviews'
import ReviewImageUpload from '@/components/ReviewImageUpload.vue'
import { useUserAuthStore } from '@/stores/user-auth'

const route = useRoute()
const router = useRouter()
const auth = useUserAuthStore()
const eligibility = ref<ReviewEligibility | null>(null)
const productImage = ref<string | null>(null)
const rating = ref(5)
const content = ref('')
const anonymous = ref(false)
const imageFileIds = ref<string[]>([])
const uploadBusy = ref(false)
const loading = ref(true)
const error = ref('')
const busy = ref(false)

const ratingCopy = computed(() => ({ 1: '非常不满意', 2: '不太满意', 3: '一般', 4: '比较满意', 5: '非常满意' }[rating.value] ?? ''))

async function load() {
  loading.value = true
  error.value = ''
  try {
    const result = (await getReviewEligibility(String(route.params.orderItemId), auth.accessToken!)).data
    eligibility.value = result
    try {
      const order = (await getMyOrder(result.order_id, auth.accessToken!)).data
      productImage.value = order.items.find((item) => item.order_item_id === result.order_item_id)?.image_url ?? null
    } catch { productImage.value = null }
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function submit() {
  if (!eligibility.value?.eligible || uploadBusy.value) return
  busy.value = true
  error.value = ''
  try {
    await createReview(eligibility.value.order_item_id, { rating: rating.value, content: content.value.trim() || null, is_anonymous: anonymous.value, image_file_ids: imageFileIds.value }, auth.accessToken!)
    await router.replace('/me/reviews?view=published')
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}

onMounted(load)
</script>

<template>
  <main class="review-create-page page-shell">
    <RouterLink class="back-link" to="/me/orders?view=pending_review">← 返回待评价订单</RouterLink>
    <header class="review-heading"><div><p class="eyebrow">分享真实体验</p><h1>评价商品</h1><p>你的评价会帮助其他顾客，也会帮助商家持续改进。</p></div><span aria-hidden="true">✦</span></header>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>

    <div v-if="loading" class="review-loading">正在准备评价内容…</div>
    <form v-else-if="eligibility?.eligible" class="review-layout" @submit.prevent="submit">
      <section class="review-main">
        <article class="review-product-card">
          <span><img v-if="productImage" :src="resolveApiAssetUrl(productImage) ?? ''" :alt="eligibility.product_name" /><i v-else>商品</i></span>
          <div><small>正在评价</small><h2>{{ eligibility.product_name }}</h2><p>款式：{{ eligibility.sku_name }}</p></div>
        </article>

        <article class="review-editor-card">
          <div class="rating-row"><div><h2>商品满意度</h2><p>点击星星选择你的真实感受</p></div><strong>{{ ratingCopy }}</strong></div>
          <fieldset class="star-picker"><legend class="sr-only">商品评分</legend><button v-for="score in 5" :key="score" type="button" :class="{ active: score <= rating }" :aria-label="`${score} 星`" :aria-pressed="score === rating" @click="rating = score">★</button></fieldset>
          <label class="review-content-field"><span>说说你的使用感受 <small>选填</small></span><textarea v-model="content" rows="7" maxlength="500" placeholder="可以聊聊商品质量、款式体验、包装或服务，让评价更有参考价值…" /><small>{{ content.length }} / 500</small></label>
          <section class="review-upload"><header><strong>添加图片</strong><small>选填，最多数量以系统提示为准</small></header><ReviewImageUpload v-model="imageFileIds" :disabled="busy" @busy-change="uploadBusy = $event" /></section>
        </article>
      </section>

      <aside class="review-submit-card">
        <p class="eyebrow">发布设置</p><h2>{{ rating }} 星 · {{ ratingCopy }}</h2>
        <label class="anonymous-choice"><input v-model="anonymous" type="checkbox" /><span><b>匿名评价</b><small>公开展示时隐藏你的用户名</small></span></label>
        <ul><li>请根据真实购买与使用体验评价</li><li>评价发布后，可在规定期限内编辑或追评</li><li>请勿包含手机号、地址等个人信息</li></ul>
        <button type="submit" :disabled="busy || uploadBusy">{{ busy ? '正在发布…' : uploadBusy ? '图片处理中…' : '发布评价' }}</button>
        <RouterLink :to="`/me/orders/${eligibility.order_id}`">暂不评价，返回订单</RouterLink>
      </aside>
    </form>
    <section v-else-if="eligibility" class="review-unavailable"><span>i</span><h2>当前无法评价</h2><p>{{ eligibility.reason_message || '该商品暂不符合评价条件。' }}</p><RouterLink to="/me/orders">返回我的订单</RouterLink></section>
  </main>
</template>

<style scoped>
.review-create-page { display: grid; gap: 20px; }.review-heading { padding: 28px 32px; display: flex; justify-content: space-between; align-items: center; color: #fff; border-radius: 22px; background: linear-gradient(125deg,#172b62,#3158d8 70%,#5578df); }.review-heading h1,.review-heading p { margin: 0; }.review-heading h1 { margin-block: 4px 7px; }.review-heading .eyebrow { color: #cbd7ff; }.review-heading > div > p:last-child { color: #dde5ff; }.review-heading > span { width: 62px; height: 62px; display: grid; place-items: center; border: 1px solid rgb(255 255 255 / 25%); border-radius: 20px; background: rgb(255 255 255 / 10%); font-size: 2rem; }
.review-layout { display: grid; grid-template-columns: minmax(0,1fr) minmax(290px,340px); align-items: start; gap: 20px; }.review-main { display: grid; gap: 18px; }.review-product-card,.review-editor-card,.review-submit-card { padding: 22px; border: 1px solid #dfe4ed; border-radius: 18px; background: #fff; box-shadow: 0 10px 32px rgb(27 49 101 / 6%); }.review-product-card { min-width: 0; display: grid; grid-template-columns: 84px minmax(0,1fr); align-items: center; gap: 16px; }.review-product-card > span,.review-product-card img,.review-product-card i { width: 84px; height: 84px; }.review-product-card img,.review-product-card i { display: grid; place-items: center; border-radius: 13px; object-fit: cover; background: #edf0f6; font-style: normal; font-size: .75rem; }.review-product-card div { min-width: 0; }.review-product-card h2,.review-product-card p { margin: 0; }.review-product-card h2 { margin-block: 4px 7px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 1.05rem; }.review-product-card small,.review-product-card p { color: #7a8599; }
.review-editor-card { display: grid; gap: 22px; }.rating-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }.rating-row h2,.rating-row p { margin: 0; }.rating-row p { margin-top: 4px; color: #7a8599; font-size: .78rem; }.rating-row strong { color: #c27612; }.star-picker { margin: 0; padding: 18px; display: flex; justify-content: center; gap: 10px; border: 0; border-radius: 14px; background: #fff9ed; }.star-picker button { min-width: 0; padding: 0; color: #d9dde5; border: 0; background: transparent; font-size: 2.4rem; line-height: 1; transition: transform .15s,color .15s; }.star-picker button:hover { transform: translateY(-2px); }.star-picker button.active { color: #f3ab2c; }.review-content-field { display: grid; gap: 8px; }.review-content-field > span { font-weight: 750; }.review-content-field span small { color: #929bad; font-weight: 500; }.review-content-field textarea { resize: vertical; }.review-content-field > small { justify-self: end; color: #8a94a7; }.review-upload { display: grid; gap: 11px; }.review-upload header { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }.review-upload small { color: #8a94a7; }
.review-submit-card { position: sticky; top: 92px; display: grid; gap: 16px; }.review-submit-card h2,.review-submit-card p { margin: 0; }.anonymous-choice { padding: 13px; display: flex; align-items: flex-start; gap: 10px; border-radius: 12px; background: #f6f8fc; }.anonymous-choice input { margin-top: 3px; }.anonymous-choice span { display: grid; gap: 4px; }.anonymous-choice small { color: #7b8498; }.review-submit-card ul { margin: 0; padding-left: 20px; display: grid; gap: 8px; color: #717d92; font-size: .78rem; line-height: 1.5; }.review-submit-card > a { text-align: center; }.review-loading,.review-unavailable { min-height: 320px; display: grid; place-items: center; align-content: center; gap: 12px; color: #788398; }.review-unavailable > span { width: 46px; height: 46px; display: grid; place-items: center; color: #3158d8; border-radius: 50%; background: #edf1ff; font-weight: 900; }.review-unavailable h2,.review-unavailable p { margin: 0; }
@media (max-width: 820px) { .review-layout { grid-template-columns: 1fr; }.review-submit-card { position: static; } }
@media (max-width: 560px) { .review-heading,.review-product-card,.review-editor-card,.review-submit-card { padding: 18px; }.review-heading > span { display: none; }.star-picker { gap: 5px; }.star-picker button { font-size: 2rem; }.rating-row { align-items: flex-start; flex-direction: column; } }
</style>
