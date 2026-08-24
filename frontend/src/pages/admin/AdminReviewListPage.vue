<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { listAdminReviews, type AdminReview } from '@/api/admin-reviews'
import { errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
const auth = useAdminAuthStore(), items = ref<AdminReview[]>([]), status = ref(''), error = ref('')
async function load() { try { items.value = (await listAdminReviews(auth.accessToken!, status.value || undefined)).data.items } catch (cause) { error.value = errorMessage(cause) } }
onMounted(load); watch(status, load)
</script>
<template><section><p class="eyebrow">内容治理</p><h1>评价管理</h1><label>状态<select v-model="status"><option value="">全部</option><option value="published">已发布</option><option value="hidden">已屏蔽</option><option value="pending">待审核</option><option value="rejected">未通过</option></select></label><p v-if="error" class="alert error">{{ error }}</p><div class="stack"><RouterLink v-for="item in items" :key="item.review_id" class="card" :to="`/admin/reviews/${item.review_id}`"><strong>{{ item.product_name }} · {{ item.rating }} 星</strong><span class="badge">{{ item.review_status }}</span><p>{{ item.content || '用户未填写文字评价' }}</p><small>{{ item.store_name }} · {{ item.user_name }}</small></RouterLink><p v-if="!items.length && !error">暂无评价。</p></div></section></template>
