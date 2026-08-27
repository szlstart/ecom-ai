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

type ManagementPortal = 'admin' | 'merchant'

export const useAdminAuthStore = defineStore('admin-auth', () => {
  const accessToken = ref<string | null>(null)
  const csrfToken = ref<string | null>(null)
  const portal = ref<ManagementPortal | null>(null)
  const permissions = ref<string[]>([])
  const scopes = ref<Array<{ scope_type: string; scope_id: number }>>([])
  const userId = ref<string | null>(null)
  const refreshAttempted = ref<Record<ManagementPortal, boolean>>({
    admin: false,
    merchant: false,
  })
  const reauthExpiresAt = ref<string | null>(null)
  const isAuthenticated = computed(() => accessToken.value !== null)
  const isAuthenticatedFor = (expectedPortal: ManagementPortal) =>
    accessToken.value !== null && portal.value === expectedPortal

  async function merchantPasswordLogin(identifier: string, password: string, deviceName: string) {
    const response = await apiRequest<AdminBootstrap>('/merchant/auth/login', {
      method: 'POST',
      body: JSON.stringify({
        identifier,
        password,
        client: { client_type: 'web', device_name: deviceName },
      }),
    })
    accept(response.data, 'merchant')
    userId.value = response.data.session.user.user_id
  }

  async function merchantRegister(username: string, password: string, storeName: string, deviceName: string) {
    const response = await apiRequest<AdminBootstrap>('/merchant/auth/registrations', {
      method: 'POST',
      body: JSON.stringify({
        username,
        password,
        store_name: storeName,
        client: { client_type: 'web', device_name: deviceName },
      }),
    })
    accept(response.data, 'merchant')
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
    accept(response.data, 'admin')
    userId.value = response.data.session.user.user_id
  }

  function accept(bootstrap: AdminBootstrap, acceptedPortal: ManagementPortal) {
    accessToken.value = bootstrap.session.access_token
    csrfToken.value = bootstrap.session.csrf_token
    portal.value = acceptedPortal
    refreshAttempted.value[acceptedPortal] = true
    permissions.value = bootstrap.permission_codes
    scopes.value = bootstrap.scopes
  }

  async function refresh(expectedPortal: ManagementPortal): Promise<boolean> {
    if (isAuthenticatedFor(expectedPortal)) return true
    if (refreshAttempted.value[expectedPortal] && !accessToken.value) return false
    refreshAttempted.value[expectedPortal] = true
    const csrfCookie = expectedPortal === 'merchant' ? 'ecom_merchant_csrf' : 'ecom_admin_csrf'
    csrfToken.value = readCookie(csrfCookie)
    const refreshPath = expectedPortal === 'merchant'
      ? '/merchant/auth/token-refresh'
      : '/admin/auth/token-refresh'
    try {
      const response = await apiRequest<SessionBootstrap>(refreshPath, {
        method: 'POST',
        headers: csrfToken.value ? { 'X-CSRF-Token': csrfToken.value } : undefined,
      })
      accessToken.value = response.data.access_token
      csrfToken.value = response.data.csrf_token
      portal.value = expectedPortal
      await loadAuthorization()
      return true
    } catch {
      clear(false)
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

  async function logout(expectedPortal: ManagementPortal = portal.value ?? 'admin') {
    if (accessToken.value) {
      const logoutPath = expectedPortal === 'merchant'
        ? '/merchant/auth/logout'
        : '/admin/auth/logout'
      await apiRequest<void>(
        logoutPath,
        { method: 'POST', headers: { 'X-CSRF-Token': csrfToken.value ?? '' } },
        accessToken.value,
      ).catch(() => undefined)
    }
    clear()
  }

  function clear(resetRefreshAttempts = true) {
    accessToken.value = null
    csrfToken.value = null
    portal.value = null
    permissions.value = []
    scopes.value = []
    userId.value = null
    reauthExpiresAt.value = null
    if (resetRefreshAttempts) {
      refreshAttempted.value = { admin: false, merchant: false }
    }
  }

  return {
    accessToken,
    csrfToken,
    portal,
    permissions,
    scopes,
    userId,
    reauthExpiresAt,
    isAuthenticated,
    isAuthenticatedFor,
    merchantPasswordLogin,
    merchantRegister,
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
