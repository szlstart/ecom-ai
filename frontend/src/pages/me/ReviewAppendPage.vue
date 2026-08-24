<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { errorMessage } from '@/api/http'
import { appendReview, getMyReview, type MyReview } from '@/api/reviews'
import ReviewImageUpload from '@/components/ReviewImageUpload.vue'
import { useUserAuthStore } from '@/stores/user-auth'
const route = useRoute(), router = useRouter(), auth = useUserAuthStore(), item = ref<MyReview | null>(null), content = ref(''), imageFileIds = ref<string[]>([]), uploadBusy = ref(false), error = ref(''), busy = ref(false)
onMounted(async () => { try { item.value = (await getMyReview(String(route.params.reviewId), auth.accessToken!)).data } catch (cause) { error.value = errorMessage(cause) } })
async function submit() { if (!item.value || content.value.trim().length < 1 || uploadBusy.value) return; busy.value = true; try { await appendReview(item.value.review_id, content.value.trim(), imageFileIds.value, auth.accessToken!); await router.replace('/me/reviews?view=published') } catch (cause) { error.value = errorMessage(cause) } finally { busy.value = false } }
</script>
<template><main class="page-shell"><h1>追加评价</h1><p v-if="error" class="alert error">{{ error }}</p><form v-if="item" class="card stack" @submit.prevent="submit"><h2>{{ item.product_name }}</h2><blockquote>{{ item.content || '首次评价未填写文字内容' }}</blockquote><label>追评内容<textarea v-model="content" required maxlength="500" /></label><ReviewImageUpload v-model="imageFileIds" :disabled="busy || !item.available_actions.includes('append')" @busy-change="uploadBusy = $event" /><small>可追评至 {{ new Date(item.append_deadline_at).toLocaleString('zh-CN') }}；追评提交后不可再次追加。</small><button :disabled="busy || uploadBusy || !item.available_actions.includes('append')">{{ uploadBusy ? '图片处理中…' : '提交追评' }}</button></form></main></template>
