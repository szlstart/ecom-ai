import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiProblem, apiRequest, registerManagementAuthRecovery } from '@/api/http'
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

interface SharedManagementAuthState {
  access_token: string
  csrf_token: string
  portal: ManagementPortal
  permission_codes: string[]
  scopes: Array<{ scope_type: string; scope_id: number }>
  user_id: string
}

type ManagementAuthChannelMessage =
  | { type: 'state-request'; source_id: string; request_id: string; rejected_token: string | null }
  | { type: 'state-response'; source_id: string; target_id: string; request_id: string; state: SharedManagementAuthState }
  | { type: 'session-updated'; source_id: string; state: SharedManagementAuthState }
  | { type: 'session-cleared'; source_id: string }

const PEER_RESPONSE_TIMEOUT_MS = 180

export const useAdminAuthStore = defineStore('admin-auth', () => {
  const accessToken = ref<string | null>(null)
  const csrfToken = ref<string | null>(null)
  const portal = ref<ManagementPortal | null>(null)
  const permissions = ref<string[]>([])
  const scopes = ref<Array<{ scope_type: string; scope_id: number }>>([])
  const userId = ref<string | null>(null)
  const reauthExpiresAt = ref<string | null>(null)
  const tabId = crypto.randomUUID()
  const channels: Record<ManagementPortal, BroadcastChannel | null> = {
    admin: createAuthChannel('admin'),
    merchant: createAuthChannel('merchant'),
  }
  const pendingPeerRequests: Record<ManagementPortal, Map<string, (accepted: boolean) => void>> = {
    admin: new Map(),
    merchant: new Map(),
  }
  const refreshInFlight: Record<ManagementPortal, Promise<boolean> | null> = {
    admin: null,
    merchant: null,
  }
  const knownAccessTokens = new Map<string, ManagementPortal>()
  const isAuthenticated = computed(() => isUsableAccessToken(accessToken.value))
  const isAuthenticatedFor = (expectedPortal: ManagementPortal) =>
    isUsableAccessToken(accessToken.value) && portal.value === expectedPortal

  function rememberAccessToken(token: string, acceptedPortal: ManagementPortal) {
    knownAccessTokens.set(token, acceptedPortal)
    if (knownAccessTokens.size > 12) {
      knownAccessTokens.delete(knownAccessTokens.keys().next().value as string)
    }
  }

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
  }

  function accept(bootstrap: AdminBootstrap, acceptedPortal: ManagementPortal, broadcast = true) {
    rememberAccessToken(bootstrap.session.access_token, acceptedPortal)
    accessToken.value = bootstrap.session.access_token
    csrfToken.value = bootstrap.session.csrf_token
    portal.value = acceptedPortal
    permissions.value = bootstrap.permission_codes
    scopes.value = bootstrap.scopes
    userId.value = bootstrap.session.user.user_id
    if (broadcast) broadcastState(acceptedPortal)
  }

  function acceptSharedState(state: SharedManagementAuthState) {
    rememberAccessToken(state.access_token, state.portal)
    accessToken.value = state.access_token
    csrfToken.value = state.csrf_token
    portal.value = state.portal
    permissions.value = [...state.permission_codes]
    scopes.value = state.scopes.map((scope) => ({ ...scope }))
    userId.value = state.user_id
  }

  function currentSharedState(expectedPortal: ManagementPortal): SharedManagementAuthState | null {
    if (!isAuthenticatedFor(expectedPortal) || !accessToken.value || !userId.value) return null
    return {
      access_token: accessToken.value,
      csrf_token: readCookie(csrfCookieName(expectedPortal)) ?? csrfToken.value ?? '',
      portal: expectedPortal,
      permission_codes: [...permissions.value],
      scopes: scopes.value.map((scope) => ({ ...scope })),
      user_id: userId.value,
    }
  }

  function broadcastState(expectedPortal: ManagementPortal) {
    const state = currentSharedState(expectedPortal)
    if (state) {
      channels[expectedPortal]?.postMessage({
        type: 'session-updated',
        source_id: tabId,
        state,
      } satisfies ManagementAuthChannelMessage)
    }
  }

  function clearLocal(expectedPortal: ManagementPortal | null = portal.value) {
    if (expectedPortal && portal.value && portal.value !== expectedPortal) return
    accessToken.value = null
    csrfToken.value = null
    portal.value = null
    permissions.value = []
    scopes.value = []
    userId.value = null
    reauthExpiresAt.value = null
    for (const [token, tokenPortal] of knownAccessTokens) {
      if (!expectedPortal || tokenPortal === expectedPortal) knownAccessTokens.delete(token)
    }
  }

  function clear(broadcast = true, expectedPortal: ManagementPortal | null = portal.value) {
    clearLocal(expectedPortal)
    if (broadcast && expectedPortal) {
      channels[expectedPortal]?.postMessage({
        type: 'session-cleared',
        source_id: tabId,
      } satisfies ManagementAuthChannelMessage)
    }
  }

  function requestPeerState(
    expectedPortal: ManagementPortal,
    rejectedToken: string | null = null,
  ): Promise<boolean> {
    const channel = channels[expectedPortal]
    if (!channel) return Promise.resolve(false)
    const requestId = crypto.randomUUID()
    return new Promise((resolve) => {
      const timer = window.setTimeout(() => {
        pendingPeerRequests[expectedPortal].delete(requestId)
        resolve(false)
      }, PEER_RESPONSE_TIMEOUT_MS)
      pendingPeerRequests[expectedPortal].set(requestId, (accepted) => {
        window.clearTimeout(timer)
        pendingPeerRequests[expectedPortal].delete(requestId)
        resolve(accepted)
      })
      channel.postMessage({
        type: 'state-request',
        source_id: tabId,
        request_id: requestId,
        rejected_token: rejectedToken,
      } satisfies ManagementAuthChannelMessage)
    })
  }

  async function refreshFromServer(expectedPortal: ManagementPortal, rotate: boolean): Promise<boolean> {
    try {
      const latestCsrfToken = readCookie(csrfCookieName(expectedPortal)) ?? csrfToken.value
      const action = rotate ? 'token-refresh' : 'session-resume'
      const response = await apiRequest<SessionBootstrap>(`/${expectedPortal}/auth/${action}`, {
        method: 'POST',
        headers: latestCsrfToken ? { 'X-CSRF-Token': latestCsrfToken } : undefined,
      })
      rememberAccessToken(response.data.access_token, expectedPortal)
      accessToken.value = response.data.access_token
      csrfToken.value = response.data.csrf_token
      portal.value = expectedPortal
      userId.value = response.data.user.user_id
      await loadAuthorization(false)
      broadcastState(expectedPortal)
      return true
    } catch (cause) {
      if (cause instanceof ApiProblem && (cause.body.status === 401 || cause.body.status === 403)) {
        clear(true, expectedPortal)
      }
      return false
    }
  }

  async function coordinateRefresh(
    expectedPortal: ManagementPortal,
    force: boolean,
    rejectedToken: string | null,
  ): Promise<boolean> {
    if (!force && isAuthenticatedFor(expectedPortal)) return true
    if (await requestPeerState(expectedPortal, rejectedToken)) return true
    const locks = navigator.locks
    if (!locks) return refreshFromServer(expectedPortal, force)
    return locks.request(`ecom-${expectedPortal}-auth-refresh-v1`, async () => {
      if (
        isAuthenticatedFor(expectedPortal)
        && (!rejectedToken || accessToken.value !== rejectedToken)
      ) return true
      if (await requestPeerState(expectedPortal, rejectedToken)) return true
      return refreshFromServer(expectedPortal, force)
    })
  }

  async function refresh(
    expectedPortal: ManagementPortal,
    force = false,
    rejectedToken: string | null = null,
  ): Promise<boolean> {
    if (!force && isAuthenticatedFor(expectedPortal)) return true
    if (refreshInFlight[expectedPortal]) return refreshInFlight[expectedPortal]
    refreshInFlight[expectedPortal] = coordinateRefresh(expectedPortal, force, rejectedToken)
    try { return await refreshInFlight[expectedPortal] }
    finally { refreshInFlight[expectedPortal] = null }
  }

  async function loadAuthorization(broadcast = true) {
    if (!accessToken.value || !portal.value) return
    const response = await apiRequest<{
      user_id: string
      permission_codes: string[]
      scopes: Array<{ scope_type: string; scope_id: number }>
    }>('/admin/me', {}, accessToken.value)
    userId.value = response.data.user_id
    permissions.value = response.data.permission_codes
    scopes.value = response.data.scopes
    if (broadcast) broadcastState(portal.value)
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
    if (accessToken.value && portal.value === expectedPortal) {
      const latestCsrfToken = readCookie(csrfCookieName(expectedPortal)) ?? csrfToken.value
      await apiRequest<void>(
        `/${expectedPortal}/auth/logout`,
        { method: 'POST', headers: { 'X-CSRF-Token': latestCsrfToken ?? '' } },
        accessToken.value,
      ).catch(() => undefined)
    }
    clear(true, expectedPortal)
  }

  function hasRefreshHint(expectedPortal: ManagementPortal): boolean {
    return readCookie(csrfCookieName(expectedPortal)) !== null
  }

  function listen(expectedPortal: ManagementPortal) {
    const channel = channels[expectedPortal]
    channel?.addEventListener('message', (event: MessageEvent<ManagementAuthChannelMessage>) => {
      const message = event.data
      if (!message || message.source_id === tabId || !acceptsPortal(expectedPortal, portal.value)) return
      if (message.type === 'state-request') {
        const state = currentSharedState(expectedPortal)
        if (state && state.access_token !== message.rejected_token) {
          channel.postMessage({
            type: 'state-response',
            source_id: tabId,
            target_id: message.source_id,
            request_id: message.request_id,
            state,
          } satisfies ManagementAuthChannelMessage)
        }
        return
      }
      if (message.type === 'state-response') {
        if (message.target_id !== tabId) return
        acceptSharedState(message.state)
        pendingPeerRequests[expectedPortal].get(message.request_id)?.(true)
        return
      }
      if (message.type === 'session-updated') {
        acceptSharedState(message.state)
        for (const resolve of pendingPeerRequests[expectedPortal].values()) resolve(true)
        return
      }
      clearLocal(expectedPortal)
    })
  }

  listen('admin')
  listen('merchant')

  registerManagementAuthRecovery(async (failedAccessToken) => {
    const expectedPortal = knownAccessTokens.get(failedAccessToken)
    if (!expectedPortal) return null
    if (
      portal.value === expectedPortal
      && accessToken.value
      && accessToken.value !== failedAccessToken
      && isUsableAccessToken(accessToken.value)
    ) return accessToken.value
    const recovered = await refresh(expectedPortal, true, failedAccessToken)
    return recovered ? accessToken.value : null
  })

  if (import.meta.hot) {
    import.meta.hot.dispose(() => {
      channels.admin?.close()
      channels.merchant?.close()
      registerManagementAuthRecovery(null)
    })
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
    hasRefreshHint,
    logout,
    clear,
  }
})

function csrfCookieName(portal: ManagementPortal): string {
  return portal === 'merchant' ? 'ecom_merchant_csrf' : 'ecom_admin_csrf'
}

function readCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`
  const item = document.cookie.split('; ').find((cookie) => cookie.startsWith(prefix))
  return item ? decodeURIComponent(item.slice(prefix.length)) : null
}

function createAuthChannel(portal: ManagementPortal): BroadcastChannel | null {
  return typeof BroadcastChannel === 'undefined'
    ? null
    : new BroadcastChannel(`ecom-${portal}-auth-v1`)
}

function acceptsPortal(expectedPortal: ManagementPortal, currentPortal: ManagementPortal | null): boolean {
  const pathPortal: ManagementPortal | null = window.location.pathname.startsWith('/merchant')
    ? 'merchant'
    : window.location.pathname.startsWith('/admin') ? 'admin' : null
  return pathPortal ? pathPortal === expectedPortal : !currentPortal || currentPortal === expectedPortal
}

function isUsableAccessToken(token: string | null, minimumValiditySeconds = 10): boolean {
  if (!token) return false
  try {
    const payload = token.split('.')[1]
    if (!payload) return true
    const normalized = payload.replace(/-/g, '+').replace(/_/g, '/')
    const decoded = JSON.parse(atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '='))) as { exp?: number }
    return typeof decoded.exp !== 'number' || decoded.exp > Date.now() / 1000 + minimumValiditySeconds
  } catch {
    return true
  }
}
