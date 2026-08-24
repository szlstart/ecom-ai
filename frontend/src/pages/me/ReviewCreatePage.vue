<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { errorMessage } from '@/api/http'
import { createReview, getReviewEligibility, type ReviewEligibility } from '@/api/reviews'
import { useUserAuthStore } from '@/stores/user-auth'
const route = useRoute(), router = useRouter(), auth = useUserAuthStore(), eligibility = ref<ReviewEligibility | null>(null), rating = ref(5), content = ref(''), anonymous = ref(false), error = ref(''), busy = ref(false)
onMounted(async () => { try { eligibility.value = (await getReviewEligibility(String(route.params.orderItemId), auth.accessToken!)).data } catch (cause) { error.value = errorMessage(cause) } })
async function submit() { if (!eligibility.value?.eligible) return; busy.value = true; try { await createReview(eligibility.value.order_item_id, { rating: rating.value, content: content.value.trim() || null, is_anonymous: anonymous.value, image_file_ids: [] }, auth.accessToken!); await router.replace('/me/reviews?view=published') } catch (cause) { error.value = errorMessage(cause) } finally { busy.value = false } }
</script>
<template><main class="page-shell"><h1>发表评价</h1><p v-if="error" class="alert error">{{ error }}</p><form v-if="eligibility?.eligible" class="card stack" @submit.prevent="submit"><h2>{{ eligibility.product_name }}</h2><label>评分<select v-model.number="rating"><option v-for="score in 5" :key="score" :value="score">{{ score }} 星</option></select></label><label>评价内容<textarea v-model="content" maxlength="500" /></label><label><input v-model="anonymous" type="checkbox" /> 公开展示时匿名</label><small>首版页面支持文字评价；图片上传组件将在对象存储直传联调后启用。</small><button :disabled="busy">{{ busy ? '提交中…' : '提交评价' }}</button></form><p v-else-if="eligibility">{{ eligibility.reason_message || '当前不可评价。' }}</p></main></template>
