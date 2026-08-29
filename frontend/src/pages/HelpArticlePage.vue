<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { getHelp, type PublishedContent } from '@/api/public-content'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
const route = useRoute(), item = ref<PublishedContent | null>(null), loading = ref(true), error = ref('')
async function load() { loading.value = true; try { item.value = (await getHelp(String(route.params.contentKey))).data } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false } }
onMounted(load)
</script>
<template><PageState :loading="loading" :error="error" :empty="!loading && !item" empty-title="帮助文章不可用" @retry="load"><article v-if="item" class="card wide-editor"><p class="eyebrow">{{ item.version.version }}</p><h1>{{ item.title }}</h1><div v-if="item.version.format === 'safe_html_v1' && item.version.html" class="safe-content" v-html="item.version.html"></div><p v-else class="preserve-lines">{{ item.version.text }}</p></article></PageState></template>
