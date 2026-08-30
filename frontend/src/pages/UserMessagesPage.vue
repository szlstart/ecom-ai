<script setup lang="ts">
import { onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'

import UserMessageCenter from '@/components/UserMessageCenter.vue'
import { useMessageCenterStore } from '@/stores/message-center'

const route = useRoute()
const center = useMessageCenterStore()

function selectRouteConversation() {
  const value = route.params.conversationId
  if (typeof value === 'string' && value) center.selectedConversationId = value
}

onMounted(selectRouteConversation)
watch(() => route.params.conversationId, selectRouteConversation)
</script>

<template>
  <section class="message-page" aria-label="消息">
    <UserMessageCenter standalone />
  </section>
</template>
