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
  session_id: string
  portal: ManagementPortal
  permission_codes: string[]
  scopes: Array<{ scope_type: string; scope_id: number }>
  user_id: string
}

type PersistedManagementAuthHint = Omit<SharedManagementAuthState, 'access_token'>

type ManagementAuthChannelMessage =
  | { type: 'state-request'; source_id: string; request_id: string; session_id: string; rejected_token: string | null }
  | { type: 'state-response'; source_id: string; target_id: string; request_id: string; state: SharedManagementAuthState }
  | { type: 'session-updated'; source_id: string; previous_session_id: string | null; state: SharedManagementAuthState }
  | { type: 'session-cleared'; source_id: string; session_id: string | null }

const PEER_RESPONSE_TIMEOUT_MS = 180

export const useAdminAuthStore = defineStore('admin-auth', () => {
  const initialHint = readPortalHint(portalFromPath())
  const accessToken = ref<string | null>(null)
  const csrfToken = ref<string | null>(initialHint?.csrf_token ?? null)
  const portal = ref<ManagementPortal | null>(initialHint?.portal ?? null)
  const permissions = ref<string[]>(initialHint?.permission_codes ?? [])
  const scopes = ref<Array<{ scope_type: string; scope_id: number }>>(initialHint?.scopes ?? [])
  const userId = ref<string | null>(initialHint?.user_id ?? null)
  const sessionId = ref<string | null>(initialHint?.session_id ?? null)
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
  const revokedSessionIds: Record<ManagementPortal, Set<string>> = {
    admin: new Set(),
    merchant: new Set(),
  }
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

  async function merchantRegister(username: string, email: string, password: string, storeName: string, captchaId: string, captchaAnswer: string, deviceName: string) {
    const response = await apiRequest<AdminBootstrap>('/merchant/auth/registrations', {
      method: 'POST',
      body: JSON.stringify({
        username,
        email,
        password,
        store_name: storeName,
        captcha_id: captchaId,
        captcha_answer: captchaAnswer,
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

  function accept(
    bootstrap: AdminBootstrap,
    acceptedPortal: ManagementPortal,
    broadcast = true,
    previousSessionId: string | null = null,
  ) {
    rememberAccessToken(bootstrap.session.access_token, acceptedPortal)
    accessToken.value = bootstrap.session.access_token
    csrfToken.value = bootstrap.session.csrf_token
    portal.value = acceptedPortal
    permissions.value = bootstrap.permission_codes
    scopes.value = bootstrap.scopes
    userId.value = bootstrap.session.user.user_id
    sessionId.value = bootstrap.session.session.session_id
    revokedSessionIds[acceptedPortal].delete(bootstrap.session.session.session_id)
    persistCurrentHint()
    if (broadcast) broadcastState(acceptedPortal, previousSessionId)
  }

  function acceptSharedState(state: SharedManagementAuthState): boolean {
    if (revokedSessionIds[state.portal].has(state.session_id)) return false
    rememberAccessToken(state.access_token, state.portal)
    accessToken.value = state.access_token
    csrfToken.value = state.csrf_token
    portal.value = state.portal
    permissions.value = [...state.permission_codes]
    scopes.value = state.scopes.map((scope) => ({ ...scope }))
    userId.value = state.user_id
    sessionId.value = state.session_id
    persistCurrentHint()
    return true
  }

  function currentSharedState(expectedPortal: ManagementPortal): SharedManagementAuthState | null {
    if (
      !isAuthenticatedFor(expectedPortal)
      || !accessToken.value
      || !userId.value
      || !sessionId.value
    ) return null
    return {
      access_token: accessToken.value,
      csrf_token: csrfToken.value ?? '',
      session_id: sessionId.value,
      portal: expectedPortal,
      permission_codes: [...permissions.value],
      scopes: scopes.value.map((scope) => ({ ...scope })),
      user_id: userId.value,
    }
  }

  function broadcastState(expectedPortal: ManagementPortal, previousSessionId: string | null = null) {
    const state = currentSharedState(expectedPortal)
    if (state) {
      channels[expectedPortal]?.postMessage({
        type: 'session-updated',
        source_id: tabId,
        previous_session_id: previousSessionId,
        state,
      } satisfies ManagementAuthChannelMessage)
    }
  }

  function persistCurrentHint() {
    if (!portal.value || !csrfToken.value || !sessionId.value || !userId.value) return
    writePortalHint(portal.value, {
      csrf_token: csrfToken.value,
      session_id: sessionId.value,
      portal: portal.value,
      permission_codes: [...permissions.value],
      scopes: scopes.value.map((scope) => ({ ...scope })),
      user_id: userId.value,
    })
  }

  function restorePortalHint(expectedPortal: ManagementPortal): boolean {
    if (portal.value === expectedPortal && sessionId.value && csrfToken.value) return true
    const hint = readPortalHint(expectedPortal)
    if (!hint) {
      const legacyCsrf = readCookie(csrfCookieName(expectedPortal))
      if (!legacyCsrf) return false
      accessToken.value = null
      csrfToken.value = legacyCsrf
      portal.value = expectedPortal
      permissions.value = []
      scopes.value = []
      userId.value = null
      sessionId.value = null
      return true
    }
    accessToken.value = null
    csrfToken.value = hint.csrf_token
    portal.value = hint.portal
    permissions.value = [...hint.permission_codes]
    scopes.value = hint.scopes.map((scope) => ({ ...scope }))
    userId.value = hint.user_id
    sessionId.value = hint.session_id
    return true
  }

  function clearLocal(expectedPortal: ManagementPortal | null = portal.value) {
    if (expectedPortal && portal.value && portal.value !== expectedPortal) return
    accessToken.value = null
    csrfToken.value = null
    portal.value = null
    permissions.value = []
    scopes.value = []
    userId.value = null
    sessionId.value = null
    reauthExpiresAt.value = null
    for (const [token, tokenPortal] of knownAccessTokens) {
      if (!expectedPortal || tokenPortal === expectedPortal) knownAccessTokens.delete(token)
    }
    if (expectedPortal) removePortalHint(expectedPortal)
  }

  function clear(broadcast = true, expectedPortal: ManagementPortal | null = portal.value) {
    const clearedSessionId = expectedPortal && portal.value === expectedPortal ? sessionId.value : null
    if (expectedPortal && clearedSessionId) revokedSessionIds[expectedPortal].add(clearedSessionId)
    clearLocal(expectedPortal)
    if (broadcast && expectedPortal) {
      channels[expectedPortal]?.postMessage({
        type: 'session-cleared',
        source_id: tabId,
        session_id: clearedSessionId,
      } satisfies ManagementAuthChannelMessage)
    }
  }

  function requestPeerState(
    expectedPortal: ManagementPortal,
    rejectedToken: string | null = null,
  ): Promise<boolean> {
    const channel = channels[expectedPortal]
    if (!channel || portal.value !== expectedPortal || !sessionId.value) return Promise.resolve(false)
    const selectedSessionId = sessionId.value
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
        session_id: selectedSessionId,
        rejected_token: rejectedToken,
      } satisfies ManagementAuthChannelMessage)
    })
  }

  async function refreshFromServer(expectedPortal: ManagementPortal, rotate: boolean): Promise<boolean> {
    if (!restorePortalHint(expectedPortal) || !csrfToken.value) return false
    const selectedSessionId = sessionId.value
    try {
      const action = rotate ? 'token-refresh' : 'session-resume'
      const response = await apiRequest<SessionBootstrap>(`/${expectedPortal}/auth/${action}`, {
        method: 'POST',
        headers: {
          'X-CSRF-Token': csrfToken.value,
          ...(selectedSessionId ? { 'X-Auth-Session': selectedSessionId } : {}),
        },
      })
      rememberAccessToken(response.data.access_token, expectedPortal)
      accessToken.value = response.data.access_token
      csrfToken.value = response.data.csrf_token
      portal.value = expectedPortal
      userId.value = response.data.user.user_id
      sessionId.value = response.data.session.session_id
      await loadAuthorization(false)
      persistCurrentHint()
      broadcastState(expectedPortal, rotate ? selectedSessionId : null)
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
    restorePortalHint(expectedPortal)
    if (!force && isAuthenticatedFor(expectedPortal)) return true
    if (await requestPeerState(expectedPortal, rejectedToken)) return true
    const locks = navigator.locks
    if (!locks) return refreshFromServer(expectedPortal, force)
    return locks.request(`ecom-${expectedPortal}-auth-refresh-v1:${sessionId.value ?? 'none'}`, async () => {
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
    persistCurrentHint()
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
    const token = portal.value === expectedPortal ? accessToken.value : null
    const selectedSessionId = portal.value === expectedPortal ? sessionId.value : null
    const selectedCsrfToken = portal.value === expectedPortal ? csrfToken.value : null
    // Clear and broadcast before waiting for the network. A slow logout request must
    // never leave the current UI usable or let another tab rebroadcast stale state.
    clear(true, expectedPortal)
    if (token) {
      await apiRequest<void>(
        `/${expectedPortal}/auth/logout`,
        { method: 'POST', headers: {
          'X-CSRF-Token': selectedCsrfToken ?? '',
          ...(selectedSessionId ? { 'X-Auth-Session': selectedSessionId } : {}),
        } },
        token,
      ).catch(() => undefined)
    }
  }

  function hasRefreshHint(expectedPortal: ManagementPortal): boolean {
    return restorePortalHint(expectedPortal)
  }

  function listen(expectedPortal: ManagementPortal) {
    const channel = channels[expectedPortal]
    channel?.addEventListener('message', (event: MessageEvent<ManagementAuthChannelMessage>) => {
      const message = event.data
      if (!message || message.source_id === tabId || !acceptsPortal(expectedPortal, portal.value)) return
      if (message.type === 'state-request') {
        const state = currentSharedState(expectedPortal)
        if (
          state
          && state.session_id === message.session_id
          && state.access_token !== message.rejected_token
        ) {
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
        if (message.target_id !== tabId || message.state.session_id !== sessionId.value) return
        const accepted = acceptSharedState(message.state)
        pendingPeerRequests[expectedPortal].get(message.request_id)?.(accepted)
        return
      }
      if (message.type === 'session-updated') {
        if (
          sessionId.value !== message.state.session_id
          && sessionId.value !== message.previous_session_id
        ) return
        const accepted = acceptSharedState(message.state)
        for (const resolve of pendingPeerRequests[expectedPortal].values()) resolve(accepted)
        return
      }
      if (message.session_id) revokedSessionIds[expectedPortal].add(message.session_id)
      if (message.session_id && sessionId.value !== message.session_id) return
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

function storageKey(portal: ManagementPortal): string {
  return `ecom:${portal}-auth:tab:v2`
}

function csrfCookieName(portal: ManagementPortal): string {
  return portal === 'merchant' ? 'ecom_merchant_csrf' : 'ecom_admin_csrf'
}

function readCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`
  const item = document.cookie.split('; ').find((cookie) => cookie.startsWith(prefix))
  return item ? decodeURIComponent(item.slice(prefix.length)) : null
}

function portalFromPath(): ManagementPortal | null {
  return window.location.pathname.startsWith('/merchant')
    ? 'merchant'
    : window.location.pathname.startsWith('/admin') ? 'admin' : null
}

function readPortalHint(portal: ManagementPortal | null): PersistedManagementAuthHint | null {
  if (!portal) return null
  try {
    const raw = window.sessionStorage.getItem(storageKey(portal))
    if (!raw) return null
    const value = JSON.parse(raw) as Partial<PersistedManagementAuthHint>
    if (
      value.portal !== portal
      || !value.session_id
      || !value.csrf_token
      || !value.user_id
      || !Array.isArray(value.permission_codes)
      || !Array.isArray(value.scopes)
    ) return null
    return value as PersistedManagementAuthHint
  } catch {
    return null
  }
}

function writePortalHint(portal: ManagementPortal, value: PersistedManagementAuthHint): void {
  try { window.sessionStorage.setItem(storageKey(portal), JSON.stringify(value)) }
  catch { /* The in-memory management session remains usable. */ }
}

function removePortalHint(portal: ManagementPortal): void {
  try { window.sessionStorage.removeItem(storageKey(portal)) }
  catch { /* Ignore unavailable browser storage. */ }
}

function createAuthChannel(portal: ManagementPortal): BroadcastChannel | null {
  return typeof BroadcastChannel === 'undefined'
    ? null
    : new BroadcastChannel(`ecom-${portal}-auth-v1`)
}

function acceptsPortal(expectedPortal: ManagementPortal, currentPortal: ManagementPortal | null): boolean {
  const pathPortal = portalFromPath()
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
