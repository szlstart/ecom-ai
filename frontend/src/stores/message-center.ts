import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useMessageCenterStore = defineStore('message-center', () => {
  const open = ref(false)
  const selectedConversationId = ref<string | null>(null)

  function show(conversationId?: string | null) {
    if (conversationId) selectedConversationId.value = conversationId
    open.value = true
  }

  function close() {
    open.value = false
  }

  return { open, selectedConversationId, show, close }
})
