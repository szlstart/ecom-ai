import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useMessageCenterStore = defineStore('message-center', () => {
  const selectedConversationId = ref<string | null>(null)
  return { selectedConversationId }
})
