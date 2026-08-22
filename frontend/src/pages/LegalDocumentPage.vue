<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { apiRequest, errorMessage } from '@/api/http'
const route = useRoute(), document = ref<{ title: string; document_version: string; safe_content: string; effective_at: string } | null>(null), error = ref('')
onMounted(async () => {
  const type = String(route.params.documentType), version = String(route.query.version ?? '')
  try { document.value = (await apiRequest<typeof document.value>(`/content/legal-documents/${encodeURIComponent(type)}?version=${encodeURIComponent(version)}`)).data }
  catch (reason) { error.value = errorMessage(reason) }
})
</script>
<template><article><p v-if="error" class="alert error">{{ error }}</p><template v-if="document"><p class="eyebrow">版本 {{ document.document_version }}</p><h1>{{ document.title }}</h1><p class="muted">生效时间：{{ document.effective_at }}</p><div class="legal-copy">{{ document.safe_content }}</div></template></article></template>
