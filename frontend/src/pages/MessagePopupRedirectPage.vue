<script setup lang="ts">
import { onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useMessageCenterStore } from '@/stores/message-center'

const route = useRoute()
const router = useRouter()
const messageCenter = useMessageCenterStore()

onMounted(async () => {
  const conversationId = typeof route.params.conversationId === 'string'
    ? route.params.conversationId
    : null
  messageCenter.show(conversationId)
  await router.replace('/')
})
</script>

<template>
  <p class="muted" role="status">正在打开消息弹窗…</p>
</template>
