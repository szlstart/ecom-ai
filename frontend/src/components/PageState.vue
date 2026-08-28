<script setup lang="ts">
import { ref, watch } from 'vue'

const props = withDefaults(defineProps<{
  loading?: boolean
  error?: string
  empty?: boolean
  emptyTitle?: string
  emptyDetail?: string
  showRefreshStatus?: boolean
}>(), { showRefreshStatus: true })
defineEmits<{ retry: [] }>()

// Initial loading may replace the page with a full loading state. Once content has
// been shown, subsequent background refreshes must keep that content mounted so
// the viewport, focused controls and unsaved input are not destroyed.
const hasSettled = ref(!props.loading)
watch(() => props.loading, (loading) => {
  if (!loading) hasSettled.value = true
})
</script>

<template>
  <div v-if="loading && !hasSettled" class="page-state" role="status" aria-live="polite">
    <span class="spinner" aria-hidden="true"></span>
    <p>正在加载，请稍候…</p>
  </div>
  <template v-else>
    <div v-if="loading && showRefreshStatus" class="page-refresh-status" role="status" aria-live="polite">
      <span class="spinner" aria-hidden="true"></span>
      <span>正在更新…</span>
    </div>
    <div v-if="error" class="page-state" role="alert">
      <h2>暂时无法加载</h2>
      <p>{{ error }}</p>
      <button type="button" @click="$emit('retry')">重新加载</button>
    </div>
    <div v-else-if="empty" class="page-state empty-state">
      <h2>{{ emptyTitle || '这里暂时没有内容' }}</h2>
      <p class="muted">{{ emptyDetail }}</p>
      <slot name="action"></slot>
    </div>
    <slot v-else></slot>
  </template>
</template>
