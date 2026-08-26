<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { getAdminReview, replyAdminReview, type AdminReview } from '@/api/admin-reviews'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const route = useRoute()
const item = ref<AdminReview | null>(null)
const etag = ref('')
const reply = ref('')
const error = ref('')
const notice = ref('')
const loading = ref(true)
const busy = ref(false)

async function load() {
  loading.value = true; error.value = ''
  try { const response = await getAdminReview(String(route.params.reviewId), auth.accessToken!); item.value = response.data; etag.value = response.headers.get('etag') ?? '' }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function submitReply() {
  if (!item.value || reply.value.trim().length < 2) return
  busy.value = true; error.value = ''; notice.value = ''
  try { const response = await replyAdminReview(item.value.review_id, etag.value, reply.value.trim(), auth.accessToken!); item.value = response.data; etag.value = response.headers.get('etag') ?? ''; reply.value = ''; notice.value = '回复已经发布，用户可以在商品评价中看到。' }
  catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}

onMounted(load)
</script>

<template>
  <section class="merchant-page-stack">
    <header class="merchant-page-heading"><div><p class="eyebrow">评价详情</p><h1>{{ item?.product_name || '用户评价' }}</h1></div><RouterLink to="/merchant/reviews">返回评价列表</RouterLink></header>
    <p v-if="notice" class="alert success">{{ notice }}</p>
    <PageState :loading="loading" :error="error" :empty="!loading && !item" empty-title="评价不存在" @retry="load"><div v-if="item" class="merchant-review-detail"><article class="card"><span class="merchant-stars large">{{ '★'.repeat(item.rating) }}{{ '☆'.repeat(5 - item.rating) }}</span><blockquote>{{ item.content || '用户仅进行了星级评价，没有填写文字。' }}</blockquote><p class="muted">{{ item.is_anonymous ? '匿名用户' : item.user_name }} · {{ new Date(item.submitted_at).toLocaleString('zh-CN') }}</p><dl><dt>商品规格</dt><dd>{{ item.sku_name }}</dd><dt>订单编号</dt><dd>{{ item.order_id }}</dd></dl></article><article class="card merchant-reply-panel"><p class="eyebrow">店铺公开回复</p><template v-if="item.merchant_reply"><h2>已经回复</h2><p>{{ item.merchant_reply.content }}</p><small>发布于 {{ new Date(item.merchant_reply.published_at).toLocaleString('zh-CN') }}</small></template><form v-else @submit.prevent="submitReply"><h2>回复这条评价</h2><p class="muted">回复发布后不可直接修改，请确认语气友善、内容准确。</p><label>回复内容<textarea v-model.trim="reply" required minlength="2" maxlength="500" placeholder="感谢您的反馈……" /></label><div class="merchant-reply-presets"><button type="button" class="secondary small" @click="reply = '感谢您的支持与认可，我们会继续认真做好商品和服务。'">感谢好评</button><button type="button" class="secondary small" @click="reply = '感谢您的反馈，很抱歉这次没有达到您的预期。我们会认真核查并持续改进。'">回应建议</button></div><button :disabled="busy || reply.length < 2">{{ busy ? '正在发布…' : '发布店铺回复' }}</button></form></article></div></PageState>
  </section>
</template>
