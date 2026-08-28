import { createPinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionBootstrap } from '@/stores/user-auth'
import { useUserAuthStore } from '@/stores/user-auth'

class FakeBroadcastChannel extends EventTarget {
  static readonly instances = new Set<FakeBroadcastChannel>()

  constructor(readonly name: string) {
    super()
    FakeBroadcastChannel.instances.add(this)
  }

  postMessage(data: unknown): void {
    for (const channel of FakeBroadcastChannel.instances) {
      if (channel !== this && channel.name === this.name) {
        queueMicrotask(() => channel.dispatchEvent(new MessageEvent('message', { data })))
      }
    }
  }

  close(): void {
    FakeBroadcastChannel.instances.delete(this)
  }

  static reset(): void {
    FakeBroadcastChannel.instances.clear()
  }
}

function bootstrap(accessToken: string): SessionBootstrap {
  return {
    user: {
      user_id: 'usr_test',
      username: 'two-tabs',
      nickname: '双标签用户',
      avatar_url: null,
      account_status: 'active',
    },
    session: {
      session_id: 'ses_test',
      client_type: 'web',
      device_name: null,
      audience: 'user',
      authenticated_at: '2026-08-28T00:00:00Z',
      last_seen_at: '2026-08-28T00:00:00Z',
      expires_at: '2026-09-28T00:00:00Z',
      is_current: true,
    },
    access_token: accessToken,
    token_type: 'Bearer',
    expires_in: 900,
    csrf_token: 'csrf-test',
  }
}

describe('user auth cross-tab synchronization', () => {
  beforeEach(() => {
    vi.stubGlobal('BroadcastChannel', FakeBroadcastChannel)
  })

  afterEach(() => {
    FakeBroadcastChannel.reset()
    vi.unstubAllGlobals()
  })

  it('lets a new tab reuse the existing access token without rotating the refresh session', async () => {
    const serverRefresh = vi.spyOn(globalThis, 'fetch')
    const firstTab = useUserAuthStore(createPinia())
    firstTab.accept(bootstrap('shared-user-token'))
    const secondTab = useUserAuthStore(createPinia())

    const restored = await secondTab.refresh()

    expect(restored).toBe(true)
    expect(secondTab.accessToken).toBe('shared-user-token')
    expect(secondTab.user?.username).toBe('two-tabs')
    expect(serverRefresh).not.toHaveBeenCalled()
  })

  it('resumes the current server session without rotation when no peer tab is available', async () => {
    const response = bootstrap('resumed-user-token')
    const serverResume = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(
      JSON.stringify({ data: response, meta: { request_id: 'req_resume', pagination: null } }),
      { status: 200, headers: { 'content-type': 'application/json' } },
    ))
    const onlyTab = useUserAuthStore(createPinia())

    const restored = await onlyTab.refresh()

    expect(restored).toBe(true)
    expect(onlyTab.accessToken).toBe('resumed-user-token')
    expect(serverResume).toHaveBeenCalledOnce()
    expect(String(serverResume.mock.calls[0]?.[0])).toContain('/auth/session-resume')
  })

  it('broadcasts a rotated token so another tab can continue using the session', async () => {
    const firstTab = useUserAuthStore(createPinia())
    const secondTab = useUserAuthStore(createPinia())
    firstTab.accept(bootstrap('old-user-token'))
    await secondTab.refresh()

    firstTab.accept(bootstrap('rotated-user-token'))
    await new Promise<void>((resolve) => queueMicrotask(() => resolve()))

    expect(secondTab.accessToken).toBe('rotated-user-token')
    expect(secondTab.isAuthenticated).toBe(true)
  })

  it('uses refresh-token rotation only after an access token is rejected', async () => {
    const onlyTab = useUserAuthStore(createPinia())
    onlyTab.accept(bootstrap('rejected-user-token'))
    const rotated = bootstrap('rotated-after-rejection')
    const serverRefresh = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(
      JSON.stringify({ data: rotated, meta: { request_id: 'req_rotate', pagination: null } }),
      { status: 200, headers: { 'content-type': 'application/json' } },
    ))

    const restored = await onlyTab.refresh(true, 'rejected-user-token')

    expect(restored).toBe(true)
    expect(onlyTab.accessToken).toBe('rotated-after-rejection')
    expect(String(serverRefresh.mock.calls[0]?.[0])).toContain('/auth/token-refresh')
  })
})
