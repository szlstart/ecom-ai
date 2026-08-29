<script setup lang="ts">
import { computed } from 'vue'

import type { SafeContent } from '@/api/catalog'
import { resolveApiAssetUrl } from '@/api/http'

const props = defineProps<{ content: SafeContent }>()
const blocks = computed(() => props.content.safe_blocks ?? [])

function kind(block: Record<string, unknown>) { return typeof block.type === 'string' ? block.type : '' }
function text(block: Record<string, unknown>) { return typeof block.text === 'string' ? block.text : '' }
function level(block: Record<string, unknown>) { return block.level === 3 ? 3 : 2 }
function items(block: Record<string, unknown>) { return Array.isArray(block.items) ? block.items.filter((item): item is string => typeof item === 'string') : [] }
function alt(block: Record<string, unknown>) { return typeof block.alt === 'string' ? block.alt : '' }
function imageUrl(block: Record<string, unknown>) {
  return typeof block.file_id === 'string' ? resolveApiAssetUrl(`/api/v1/files/${block.file_id}`) : null
}
</script>

<template>
  <div v-if="content.content_format === 'structured_v1' && blocks.length" class="safe-content-blocks">
    <template v-for="(block, index) in blocks" :key="`${kind(block)}-${index}`">
      <p v-if="kind(block) === 'paragraph'">{{ text(block) }}</p>
      <h2 v-else-if="kind(block) === 'heading' && level(block) === 2">{{ text(block) }}</h2>
      <h3 v-else-if="kind(block) === 'heading'">{{ text(block) }}</h3>
      <ul v-else-if="kind(block) === 'bullet_list'"><li v-for="(item, itemIndex) in items(block)" :key="itemIndex">{{ item }}</li></ul>
      <figure v-else-if="kind(block) === 'image'"><img :src="imageUrl(block) || undefined" :alt="alt(block)" loading="lazy" decoding="async" /></figure>
    </template>
  </div>
  <div v-else class="safe-content">{{ content.safe_text_fallback }}</div>
</template>
