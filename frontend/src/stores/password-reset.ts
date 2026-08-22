import { defineStore } from 'pinia'
import { ref } from 'vue'
export const usePasswordResetStore = defineStore('password-reset', () => {
  const ticket = ref<string | null>(null)
  function setTicket(value: string) { ticket.value = value }
  function clear() { ticket.value = null }
  return { ticket, setTicket, clear }
})
