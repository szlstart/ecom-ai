import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { apiRequest } from '@/api/http'
import type { SessionBootstrap } from '@/stores/user-auth'

interface AdminBootstrap {
  session: SessionBootstrap
  permission_codes: string[]
  scopes: Array<{ scope_type: string; scope_id: number }>
}

interface ReauthenticationResult {
  reauth_expires_at: string
  assurance_level: string
}

export const useAdminAuthStore = defineStore('admin-auth', () => {
  const accessToken = ref<string | null>(null)
  const csrfToken = ref<string | null>(readCookie('ecom_admin_csrf'))
  const permissions = ref<string[]>([])
  const scopes = ref<Array<{ scope_type: string; scope_id: number }>>([])
  const userId = ref<string | null>(null)
  const refreshAttempted = ref(false)
  const reauthExpiresAt = ref<string | null>(null)
  const isAuthenticated = computed(() => accessToken.value !== null)

  async function merchantPasswordLogin(identifier: string, password: string, deviceName: string) {
    const response = await apiRequest<AdminBootstrap>('/merchant/auth/login', {
      method: 'POST',
      body: JSON.stringify({
        identifier,
        password,
        client: { client_type: 'web', device_name: deviceName },
      }),
    })
    accept(response.data)
    userId.value = response.data.session.user.user_id
  }

  async function platformPasswordLogin(identifier: string, password: string, deviceName: string) {
    const response = await apiRequest<AdminBootstrap>('/admin/auth/password-login', {
      method: 'POST',
      body: JSON.stringify({
        identifier,
        password,
        client: { client_type: 'web', device_name: deviceName },
      }),
    })
    accept(response.data)
    userId.value = response.data.session.user.user_id
  }

  function accept(bootstrap: AdminBootstrap) {
    accessToken.value = bootstrap.session.access_token
    csrfToken.value = bootstrap.session.csrf_token
    permissions.value = bootstrap.permission_codes
    scopes.value = bootstrap.scopes
  }

  async function refresh(): Promise<boolean> {
    if (refreshAttempted.value && !accessToken.value) return false
    refreshAttempted.value = true
    try {
      const response = await apiRequest<SessionBootstrap>('/admin/auth/token-refresh', {
        method: 'POST',
        headers: csrfToken.value ? { 'X-CSRF-Token': csrfToken.value } : undefined,
      })
      accessToken.value = response.data.access_token
      csrfToken.value = response.data.csrf_token
      await loadAuthorization()
      return true
    } catch {
      clear()
      return false
    }
  }

  async function loadAuthorization() {
    if (!accessToken.value) return
    const response = await apiRequest<{
      user_id: string
      permission_codes: string[]
      scopes: Array<{ scope_type: string; scope_id: number }>
    }>('/admin/me', {}, accessToken.value)
    userId.value = response.data.user_id
    permissions.value = response.data.permission_codes
    scopes.value = response.data.scopes
  }

  async function reauthenticateMerchant(password: string) {
    const response = await apiRequest<ReauthenticationResult>(
      '/merchant/auth/reauthentications',
      { method: 'POST', body: JSON.stringify({ password }) },
      accessToken.value,
    )
    reauthExpiresAt.value = response.data.reauth_expires_at
    return response.data
  }

  async function reauthenticatePlatformPassword(password: string) {
    const response = await apiRequest<ReauthenticationResult>(
      '/admin/auth/password-reauthentications',
      { method: 'POST', body: JSON.stringify({ password }) },
      accessToken.value,
    )
    reauthExpiresAt.value = response.data.reauth_expires_at
    return response.data
  }

  function has(permission: string): boolean {
    return permissions.value.includes(permission)
  }

  async function logout() {
    if (accessToken.value) {
      await apiRequest<void>(
        '/admin/auth/logout',
        { method: 'POST', headers: { 'X-CSRF-Token': csrfToken.value ?? '' } },
        accessToken.value,
      ).catch(() => undefined)
    }
    clear()
  }

  function clear() {
    accessToken.value = null
    csrfToken.value = null
    permissions.value = []
    scopes.value = []
    userId.value = null
    reauthExpiresAt.value = null
  }

  return {
    accessToken,
    csrfToken,
    permissions,
    scopes,
    userId,
    reauthExpiresAt,
    isAuthenticated,
    merchantPasswordLogin,
    platformPasswordLogin,
    refresh,
    loadAuthorization,
    reauthenticateMerchant,
    reauthenticatePlatformPassword,
    has,
    logout,
    clear,
  }
})

function readCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`
  const item = document.cookie.split('; ').find((cookie) => cookie.startsWith(prefix))
  return item ? decodeURIComponent(item.slice(prefix.length)) : null
}
