<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getAbout, getFooter, type PublishedContent } from '@/api/public-content'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
const items = ref<PublishedContent[]>([]), footer = ref<PublishedContent[]>([]), loading = ref(true), error = ref('')
async function load() { loading.value = true; try { const [about, foot] = await Promise.all([getAbout(), getFooter()]); items.value = about.data.items; footer.value = foot.data.items } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false } }
onMounted(load)
</script>
<template><PageState :loading="loading" :error="error" :empty="!loading && items.length === 0" empty-title="暂无关于内容" @retry="load"><section class="storefront-stack"><article v-for="item in items" :key="item.content_id" class="card wide-editor"><h1>{{ item.title }}</h1><p class="preserve-lines">{{ item.version.text }}</p></article><aside v-if="footer.length" class="card"><h2>平台信息</h2><p v-for="item in footer" :key="item.content_id">{{ item.version.text }}</p></aside></section></PageState></template>
