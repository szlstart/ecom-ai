<script setup lang="ts">
defineProps<{
  loading?: boolean
  error?: string
  empty?: boolean
  emptyTitle?: string
  emptyDetail?: string
}>()
defineEmits<{ retry: [] }>()
</script>

<template>
  <div v-if="loading" class="page-state" role="status" aria-live="polite">
    <span class="spinner" aria-hidden="true"></span>
    <p>正在加载，请稍候…</p>
  </div>
  <div v-else-if="error" class="page-state" role="alert">
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
