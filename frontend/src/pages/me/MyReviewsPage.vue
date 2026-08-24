<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { errorMessage } from '@/api/http'
import { listMyReviews, type MyReviewListItem } from '@/api/reviews'
import { useUserAuthStore } from '@/stores/user-auth'
const route = useRoute(), router = useRouter(), auth = useUserAuthStore(), view = ref<'pending' | 'published'>(route.query.view === 'pending' ? 'pending' : 'published'), items = ref<MyReviewListItem[]>([]), error = ref(''), loading = ref(false)
async function load() { loading.value = true; try { items.value = (await listMyReviews(view.value, auth.accessToken!)).data.items } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false } }
watch(view, async () => { await router.replace({ query: { view: view.value } }); await load() }); onMounted(load)
</script>
<template><main class="page-shell"><h1>我的评价</h1><nav class="tabs"><button :class="{ active: view === 'published' }" @click="view = 'published'">已评价</button><button :class="{ active: view === 'pending' }" @click="view = 'pending'">待评价</button></nav><p v-if="error" class="alert error">{{ error }}</p><p v-else-if="loading">正在加载…</p><div v-else class="stack"><article v-for="entry in items" :key="entry.order_item_id" class="card"><h2>{{ entry.product_name }}</h2><p>{{ entry.sku_name }}</p><template v-if="entry.review"><p>{{ entry.review.rating }} 星 · {{ entry.review.review_status }}</p><p>{{ entry.review.content || '未填写文字评价' }}</p><div class="actions"><RouterLink v-if="entry.review.available_actions.includes('edit')" :to="`/me/reviews/${entry.review.review_id}/edit`">编辑</RouterLink><RouterLink v-if="entry.review.available_actions.includes('append')" :to="`/me/reviews/${entry.review.review_id}/append`">追评</RouterLink></div></template><RouterLink v-else :to="`/me/order-items/${entry.order_item_id}/review`">发表评价</RouterLink></article><p v-if="!items.length">暂无{{ view === 'pending' ? '待评价商品' : '评价记录' }}。</p></div></main></template>
