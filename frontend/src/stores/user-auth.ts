import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { apiRequest } from '@/api/http'

export interface UserSummary {
  user_id: string
  username: string
  nickname: string
  avatar_url: string | null
  account_status: string
}

export interface SessionBootstrap {
  user: UserSummary
  session: {
    session_id: string
    client_type: string
    device_name: string | null
    audience: string
    authenticated_at: string
    last_seen_at: string
    expires_at: string
    is_current: boolean
  }
  access_token: string
  token_type: 'Bearer'
  expires_in: number
  csrf_token: string
}

export const useUserAuthStore = defineStore('user-auth', () => {
  const accessToken = ref<string | null>(null)
  const csrfToken = ref<string | null>(readCookie('ecom_user_csrf'))
  const user = ref<UserSummary | null>(null)
  const refreshAttempted = ref(false)
  const isAuthenticated = computed(() => accessToken.value !== null)

  function accept(bootstrap: SessionBootstrap) {
    accessToken.value = bootstrap.access_token
    csrfToken.value = bootstrap.csrf_token
    user.value = bootstrap.user
  }

  async function refresh(): Promise<boolean> {
    if (refreshAttempted.value && !accessToken.value) return false
    refreshAttempted.value = true
    try {
      const response = await apiRequest<SessionBootstrap>('/auth/token-refresh', {
        method: 'POST',
        headers: csrfToken.value ? { 'X-CSRF-Token': csrfToken.value } : undefined,
      })
      accept(response.data)
      return true
    } catch {
      clear()
      return false
    }
  }

  async function logout() {
    if (accessToken.value) {
      await apiRequest<void>(
        '/auth/logout',
        { method: 'POST', headers: { 'X-CSRF-Token': csrfToken.value ?? '' } },
        accessToken.value,
      ).catch(() => undefined)
    }
    clear()
  }

  function clear() {
    accessToken.value = null
    csrfToken.value = null
    user.value = null
  }

  return { accessToken, csrfToken, user, isAuthenticated, accept, refresh, logout, clear }
})

function readCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`
  const item = document.cookie.split('; ').find((cookie) => cookie.startsWith(prefix))
  return item ? decodeURIComponent(item.slice(prefix.length)) : null
}
