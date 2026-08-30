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
  <section class="message-page" aria-labelledby="user-messages-title">
    <header class="message-page-heading">
      <div><p class="eyebrow">Ecom AI 服务中心</p><h1 id="user-messages-title">消息</h1></div>
      <p>专属客服、店铺咨询与 AI 执行记录集中在同一个三栏工作台。</p>
    </header>
    <UserMessageCenter standalone />
  </section>
</template>
