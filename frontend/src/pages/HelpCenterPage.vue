<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { listHelp, type PublishedContent } from '@/api/public-content'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
const items = ref<PublishedContent[]>([]), loading = ref(true), error = ref('')
async function load() { loading.value = true; try { items.value = (await listHelp()).data.items } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false } }
onMounted(load)
</script>
<template><PageState :loading="loading" :error="error" :empty="!loading && items.length === 0" empty-title="暂无帮助文章" @retry="load"><section class="storefront-stack"><header class="page-heading"><div><p class="eyebrow">帮助中心</p><h1>帮助中心</h1><p>这里展示平台当前生效的帮助内容。</p></div></header><div class="card-list"><RouterLink v-for="item in items" :key="item.content_id" class="card" :to="`/help/${item.content_key}`"><h2>{{ item.title }}</h2><p>{{ item.version.text.slice(0, 160) }}</p></RouterLink></div></section></PageState></template>
