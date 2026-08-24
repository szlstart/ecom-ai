<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { errorMessage } from '@/api/http'
import { getMyReview, updateReview, type MyReview } from '@/api/reviews'
import ReviewImageUpload from '@/components/ReviewImageUpload.vue'
import { useUserAuthStore } from '@/stores/user-auth'
const route = useRoute(), router = useRouter(), auth = useUserAuthStore(), item = ref<MyReview | null>(null), rating = ref(5), content = ref(''), anonymous = ref(false), imageFileIds = ref<string[]>([]), uploadBusy = ref(false), error = ref(''), busy = ref(false)
onMounted(async () => { try { item.value = (await getMyReview(String(route.params.reviewId), auth.accessToken!)).data; rating.value = item.value.rating; content.value = item.value.content ?? ''; anonymous.value = item.value.is_anonymous; imageFileIds.value = item.value.images.map((image) => image.file_id) } catch (cause) { error.value = errorMessage(cause) } })
async function submit() { if (!item.value || uploadBusy.value) return; busy.value = true; try { await updateReview(item.value.review_id, item.value.version, { rating: rating.value, content: content.value.trim() || null, is_anonymous: anonymous.value, image_file_ids: imageFileIds.value }, auth.accessToken!); await router.replace('/me/reviews?view=published') } catch (cause) { error.value = errorMessage(cause) } finally { busy.value = false } }
</script>
<template><main class="page-shell"><h1>编辑评价</h1><p v-if="error" class="alert error">{{ error }}</p><form v-if="item" class="card stack" @submit.prevent="submit"><h2>{{ item.product_name }}</h2><label>评分<select v-model.number="rating"><option v-for="score in 5" :key="score" :value="score">{{ score }} 星</option></select></label><label>评价内容<textarea v-model="content" maxlength="500" /></label><ReviewImageUpload v-model="imageFileIds" :disabled="busy || !item.available_actions.includes('edit')" @busy-change="uploadBusy = $event" /><label><input v-model="anonymous" type="checkbox" /> 公开展示时匿名</label><small>可编辑至 {{ new Date(item.edit_deadline_at).toLocaleString('zh-CN') }}</small><button :disabled="busy || uploadBusy || !item.available_actions.includes('edit')">{{ uploadBusy ? '图片处理中…' : '保存修改' }}</button></form></main></template>
