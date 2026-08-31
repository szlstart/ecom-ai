import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

export const useMessageCenterStore = defineStore('message-center', () => {
  const selectedConversationId = ref<string | null>(null)
  const unreadByConversation = ref<Record<string, number>>({})
  const totalUnread = computed(() => Object.values(unreadByConversation.value).reduce((total, value) => total + value, 0))

  function replaceUnread(items: Array<{ conversation_id: string; unread_count: number }>) {
    unreadByConversation.value = Object.fromEntries(items.map((item) => [item.conversation_id, Math.max(0, item.unread_count)]))
  }
  function setUnread(conversationId: string, unreadCount: number) {
    unreadByConversation.value = { ...unreadByConversation.value, [conversationId]: Math.max(0, unreadCount) }
  }
  function removeConversation(conversationId: string) {
    const next = { ...unreadByConversation.value }
    delete next[conversationId]
    unreadByConversation.value = next
  }
  function clear() {
    selectedConversationId.value = null
    unreadByConversation.value = {}
  }

  return { selectedConversationId, unreadByConversation, totalUnread, replaceUnread, setUnread, removeConversation, clear }
})
