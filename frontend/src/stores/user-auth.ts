import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiProblem, apiRequest, registerUserAuthRecovery } from '@/api/http'

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

interface SharedAuthState {
  access_token: string
  csrf_token: string
  session_id: string
  user: UserSummary
}

interface PersistedAuthHint {
  csrf_token: string
  session_id: string
  user: UserSummary
}

type AuthChannelMessage =
  | { type: 'state-request'; source_id: string; request_id: string; session_id: string; rejected_token: string | null }
  | { type: 'state-response'; source_id: string; target_id: string; request_id: string; state: SharedAuthState }
  | { type: 'session-updated'; source_id: string; previous_session_id: string | null; state: SharedAuthState }
  | { type: 'session-cleared'; source_id: string; session_id: string }

const AUTH_CHANNEL_NAME = 'ecom-user-auth-v1'
const AUTH_REFRESH_LOCK_NAME = 'ecom-user-auth-refresh-v1'
const AUTH_TAB_STORAGE_KEY = 'ecom:user-auth:tab:v2'
const PEER_RESPONSE_TIMEOUT_MS = 180

export const useUserAuthStore = defineStore('user-auth', () => {
  const restoredHint = readPersistedHint()
  const accessToken = ref<string | null>(null)
  const csrfToken = ref<string | null>(restoredHint?.csrf_token ?? readCookie('ecom_user_csrf'))
  const sessionId = ref<string | null>(restoredHint?.session_id ?? null)
  const user = ref<UserSummary | null>(restoredHint?.user ?? null)
  const tabId = crypto.randomUUID()
  const channel = createAuthChannel()
  const pendingPeerRequests = new Map<string, (accepted: boolean) => void>()
  const knownAccessTokens = new Set<string>()
  let refreshInFlight: Promise<boolean> | null = null
  const isAuthenticated = computed(() => isUsableAccessToken(accessToken.value))

  function rememberAccessToken(token: string) {
    knownAccessTokens.add(token)
    if (knownAccessTokens.size > 8) {
      knownAccessTokens.delete(knownAccessTokens.values().next().value as string)
    }
  }

  function accept(
    bootstrap: SessionBootstrap,
    broadcast = true,
    previousSessionId: string | null = null,
  ) {
    rememberAccessToken(bootstrap.access_token)
    accessToken.value = bootstrap.access_token
    csrfToken.value = bootstrap.csrf_token
    sessionId.value = bootstrap.session.session_id
    user.value = bootstrap.user
    persistHint()
    if (broadcast) broadcastState(previousSessionId)
  }

  function acceptSharedState(state: SharedAuthState) {
    rememberAccessToken(state.access_token)
    accessToken.value = state.access_token
    csrfToken.value = state.csrf_token
    sessionId.value = state.session_id
    user.value = state.user
    persistHint()
  }

  function currentSharedState(): SharedAuthState | null {
    if (!isUsableAccessToken(accessToken.value) || !accessToken.value || !sessionId.value || !user.value) return null
    return {
      access_token: accessToken.value,
      csrf_token: csrfToken.value ?? '',
      session_id: sessionId.value,
      // Vue stores objects in deep reactive proxies. BroadcastChannel performs
      // the structured-clone algorithm and rejects proxies with DataCloneError,
      // which previously left a successful login modal open with a misleading
      // network error. Always publish a plain snapshot.
      user: { ...user.value },
    }
  }

  function broadcastState(previousSessionId: string | null) {
    const state = currentSharedState()
    if (state) channel?.postMessage({
      type: 'session-updated',
      source_id: tabId,
      previous_session_id: previousSessionId,
      state,
    } satisfies AuthChannelMessage)
  }

  function persistHint() {
    if (!csrfToken.value || !sessionId.value || !user.value) return
    writePersistedHint({
      csrf_token: csrfToken.value,
      session_id: sessionId.value,
      user: { ...user.value },
    })
  }

  function clearLocal() {
    accessToken.value = null
    csrfToken.value = null
    sessionId.value = null
    user.value = null
    knownAccessTokens.clear()
    removePersistedHint()
  }

  function clear(broadcast = true) {
    const clearedSessionId = sessionId.value
    clearLocal()
    if (broadcast && clearedSessionId) {
      channel?.postMessage({
        type: 'session-cleared',
        source_id: tabId,
        session_id: clearedSessionId,
      } satisfies AuthChannelMessage)
    }
  }

  function requestPeerState(rejectedToken: string | null = null): Promise<boolean> {
    if (!channel || !sessionId.value) return Promise.resolve(false)
    const selectedSessionId = sessionId.value
    const requestId = crypto.randomUUID()
    return new Promise((resolve) => {
      const timer = window.setTimeout(() => {
        pendingPeerRequests.delete(requestId)
        resolve(false)
      }, PEER_RESPONSE_TIMEOUT_MS)
      pendingPeerRequests.set(requestId, (accepted) => {
        window.clearTimeout(timer)
        pendingPeerRequests.delete(requestId)
        resolve(accepted)
      })
      channel.postMessage({
        type: 'state-request',
        source_id: tabId,
        request_id: requestId,
        session_id: selectedSessionId,
        rejected_token: rejectedToken,
      } satisfies AuthChannelMessage)
    })
  }

  async function refreshFromServer(rotate: boolean): Promise<boolean> {
    const selectedSessionId = sessionId.value
    if (!csrfToken.value) return false
    try {
      const response = await apiRequest<SessionBootstrap>(rotate ? '/auth/token-refresh' : '/auth/session-resume', {
        method: 'POST',
        headers: {
          'X-CSRF-Token': csrfToken.value,
          ...(selectedSessionId ? { 'X-Auth-Session': selectedSessionId } : {}),
        },
      })
      accept(response.data, true, rotate ? selectedSessionId : null)
      return true
    } catch (cause) {
      if (cause instanceof ApiProblem && (cause.body.status === 401 || cause.body.status === 403)) {
        clear()
      }
      return false
    }
  }

  async function coordinateRefresh(force: boolean, rejectedToken: string | null): Promise<boolean> {
    if (!force && isUsableAccessToken(accessToken.value)) return true
    if (await requestPeerState(rejectedToken)) return true
    const locks = navigator.locks
    if (!locks) return refreshFromServer(force)
    return locks.request(`${AUTH_REFRESH_LOCK_NAME}:${sessionId.value ?? 'none'}`, async () => {
      if (
        isUsableAccessToken(accessToken.value)
        && (!rejectedToken || accessToken.value !== rejectedToken)
      ) return true
      if (await requestPeerState(rejectedToken)) return true
      return refreshFromServer(force)
    })
  }

  async function refresh(force = false, rejectedToken: string | null = null): Promise<boolean> {
    if (!force && isUsableAccessToken(accessToken.value)) return true
    if (refreshInFlight) return refreshInFlight
    refreshInFlight = coordinateRefresh(force, rejectedToken)
    try { return await refreshInFlight }
    finally { refreshInFlight = null }
  }

  async function logout() {
    const selectedSessionId = sessionId.value
    if (accessToken.value) {
      await apiRequest<void>(
        '/auth/logout',
        { method: 'POST', headers: {
          'X-CSRF-Token': csrfToken.value ?? '',
          ...(selectedSessionId ? { 'X-Auth-Session': selectedSessionId } : {}),
        } },
        accessToken.value,
      ).catch(() => undefined)
    }
    clear()
  }

  function updateUser(patch: Partial<UserSummary>) {
    if (!user.value) return
    user.value = { ...user.value, ...patch }
    persistHint()
    broadcastState(null)
  }

  channel?.addEventListener('message', (event: MessageEvent<AuthChannelMessage>) => {
    const message = event.data
    if (!message || message.source_id === tabId) return
    if (message.type === 'state-request') {
      const state = currentSharedState()
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
        } satisfies AuthChannelMessage)
      }
      return
    }
    if (message.type === 'state-response') {
      if (message.target_id !== tabId || message.state.session_id !== sessionId.value) return
      acceptSharedState(message.state)
      pendingPeerRequests.get(message.request_id)?.(true)
      return
    }
    if (message.type === 'session-updated') {
      if (
        sessionId.value !== message.state.session_id
        && sessionId.value !== message.previous_session_id
      ) return
      acceptSharedState(message.state)
      for (const resolve of pendingPeerRequests.values()) resolve(true)
      return
    }
    if (sessionId.value === message.session_id) clearLocal()
  })

  registerUserAuthRecovery(async (failedAccessToken) => {
    // apiRequest is shared by the storefront, merchant portal and admin portal.
    // Only recover tokens that this user store has actually issued or received;
    // otherwise a merchant/admin 401 could accidentally be retried as a shopper.
    if (!knownAccessTokens.has(failedAccessToken)) return null
    if (
      accessToken.value
      && accessToken.value !== failedAccessToken
      && isUsableAccessToken(accessToken.value)
    ) return accessToken.value
    const recovered = await refresh(true, failedAccessToken)
    return recovered ? accessToken.value : null
  })

  if (import.meta.hot) {
    import.meta.hot.dispose(() => {
      channel?.close()
      registerUserAuthRecovery(null)
    })
  }

  function hasRefreshHint(): boolean {
    return Boolean(csrfToken.value)
  }

  return { accessToken, csrfToken, sessionId, user, isAuthenticated, hasRefreshHint, accept, refresh, logout, clear, updateUser }
})

function readPersistedHint(): PersistedAuthHint | null {
  try {
    const raw = window.sessionStorage.getItem(AUTH_TAB_STORAGE_KEY)
    if (!raw) return null
    const value = JSON.parse(raw) as Partial<PersistedAuthHint>
    if (!value.session_id || !value.csrf_token || !value.user?.user_id) return null
    return value as PersistedAuthHint
  } catch {
    return null
  }
}

function readCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`
  const item = document.cookie.split('; ').find((cookie) => cookie.startsWith(prefix))
  return item ? decodeURIComponent(item.slice(prefix.length)) : null
}

function writePersistedHint(value: PersistedAuthHint): void {
  try { window.sessionStorage.setItem(AUTH_TAB_STORAGE_KEY, JSON.stringify(value)) }
  catch { /* Session persistence is an enhancement; the in-memory session remains usable. */ }
}

function removePersistedHint(): void {
  try { window.sessionStorage.removeItem(AUTH_TAB_STORAGE_KEY) }
  catch { /* Ignore unavailable browser storage. */ }
}

function createAuthChannel(): BroadcastChannel | null {
  return typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel(AUTH_CHANNEL_NAME)
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
    // Tests and local component stubs use opaque placeholder tokens. Real access
    // tokens are JWTs, but an opaque value should remain usable until the API rejects it.
    return true
  }
}
