<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { listAdminReviews, type AdminReview } from '@/api/admin-reviews'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const route = useRoute()
const items = ref<AdminReview[]>([])
const loading = ref(true)
const error = ref('')
const filter = ref<'all' | 'unanswered' | 'replied'>('all')
const visible = computed(() => items.value.filter((item) => filter.value === 'all' || (filter.value === 'unanswered' ? !item.merchant_reply : Boolean(item.merchant_reply))))

async function load() {
  loading.value = true; error.value = ''
  try { items.value = (await listAdminReviews(auth.accessToken!, 'published')).data.items }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

onMounted(() => { filter.value = route.query.unanswered === '1' ? 'unanswered' : 'all'; void load() })
watch(() => route.query.unanswered, (value) => { filter.value = value === '1' ? 'unanswered' : 'all' })
</script>

<template>
  <section class="merchant-page-stack">
    <header class="merchant-page-heading"><div><p class="eyebrow">口碑维护</p><h1>用户评价</h1><p>阅读真实反馈，并以店铺身份公开回复。</p></div></header>
    <div class="merchant-segmented"><button :class="{ active: filter === 'all' }" @click="filter = 'all'">全部</button><button :class="{ active: filter === 'unanswered' }" @click="filter = 'unanswered'">待回复</button><button :class="{ active: filter === 'replied' }" @click="filter = 'replied'">已回复</button></div>
    <PageState :loading="loading" :error="error" :empty="!loading && !error && visible.length === 0" empty-title="当前没有评价" @retry="load"><div class="merchant-review-list"><RouterLink v-for="item in visible" :key="item.review_id" :to="`/merchant/reviews/${item.review_id}`" class="card merchant-review-card"><header><div><strong>{{ item.product_name }}</strong><span class="merchant-stars">{{ '★'.repeat(item.rating) }}{{ '☆'.repeat(5 - item.rating) }}</span></div><span class="badge">{{ item.merchant_reply ? '已回复' : '待回复' }}</span></header><p>{{ item.content || '用户仅进行了星级评价，没有填写文字。' }}</p><footer><span>{{ item.is_anonymous ? '匿名用户' : item.user_name }}</span><time>{{ new Date(item.submitted_at).toLocaleString('zh-CN') }}</time></footer></RouterLink></div></PageState>
  </section>
</template>
