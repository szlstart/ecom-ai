import { createPinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { registerManagementAuthRecovery } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
import type { SessionBootstrap } from '@/stores/user-auth'

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

function session(accessToken: string): SessionBootstrap {
  return {
    user: {
      user_id: 'usr_merchant_test',
      username: 'merchant-tabs',
      nickname: '商家双标签',
      avatar_url: null,
      account_status: 'active',
    },
    session: {
      session_id: 'ses_merchant_test',
      client_type: 'merchant',
      device_name: null,
      audience: 'admin',
      authenticated_at: '2026-08-28T00:00:00Z',
      last_seen_at: '2026-08-28T00:00:00Z',
      expires_at: '2026-09-28T00:00:00Z',
      is_current: true,
    },
    access_token: accessToken,
    token_type: 'Bearer',
    expires_in: 900,
    csrf_token: 'merchant-csrf-test',
  }
}

function envelope(data: unknown): Response {
  return new Response(
    JSON.stringify({ data, meta: { request_id: 'req_merchant_tabs', pagination: null } }),
    { status: 200, headers: { 'content-type': 'application/json' } },
  )
}

describe('management auth cross-tab synchronization', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/merchant/products')
    vi.stubGlobal('BroadcastChannel', FakeBroadcastChannel)
  })

  afterEach(() => {
    FakeBroadcastChannel.reset()
    registerManagementAuthRecovery(null)
    vi.unstubAllGlobals()
  })

  it('lets a second merchant tab reuse the first tab state without rotating the session', async () => {
    const server = vi.spyOn(globalThis, 'fetch').mockResolvedValue(envelope({
      session: session('shared-merchant-token'),
      permission_codes: ['products:read'],
      scopes: [{ scope_type: 'store', scope_id: 7 }],
    }))
    const firstTab = useAdminAuthStore(createPinia())
    await firstTab.merchantPasswordLogin('merchant-tabs', 'password', 'first tab')
    server.mockClear()
    const secondTab = useAdminAuthStore(createPinia())

    const restored = await secondTab.refresh('merchant')

    expect(restored).toBe(true)
    expect(secondTab.accessToken).toBe('shared-merchant-token')
    expect(secondTab.permissions).toEqual(['products:read'])
    expect(secondTab.scopes).toEqual([{ scope_type: 'store', scope_id: 7 }])
    expect(server).not.toHaveBeenCalled()
  })

  it('uses the non-rotating merchant resume endpoint when no peer tab is available', async () => {
    const server = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(envelope(session('resumed-merchant-token')))
      .mockResolvedValueOnce(envelope({
        user_id: 'usr_merchant_test',
        permission_codes: ['products:read', 'orders:read'],
        scopes: [{ scope_type: 'store', scope_id: 7 }],
      }))
    const onlyTab = useAdminAuthStore(createPinia())

    const restored = await onlyTab.refresh('merchant')

    expect(restored).toBe(true)
    expect(onlyTab.isAuthenticatedFor('merchant')).toBe(true)
    expect(String(server.mock.calls[0]?.[0])).toContain('/merchant/auth/session-resume')
    expect(String(server.mock.calls[1]?.[0])).toContain('/admin/me')
  })

  it('rotates the merchant refresh token only after its access token is rejected', async () => {
    const server = vi.spyOn(globalThis, 'fetch').mockResolvedValue(envelope({
      session: session('rejected-merchant-token'),
      permission_codes: ['products:read'],
      scopes: [{ scope_type: 'store', scope_id: 7 }],
    }))
    const onlyTab = useAdminAuthStore(createPinia())
    await onlyTab.merchantPasswordLogin('merchant-tabs', 'password', 'only tab')
    server.mockReset()
      .mockResolvedValueOnce(envelope(session('rotated-merchant-token')))
      .mockResolvedValueOnce(envelope({
        user_id: 'usr_merchant_test',
        permission_codes: ['products:read'],
        scopes: [{ scope_type: 'store', scope_id: 7 }],
      }))

    const restored = await onlyTab.refresh('merchant', true, 'rejected-merchant-token')

    expect(restored).toBe(true)
    expect(onlyTab.accessToken).toBe('rotated-merchant-token')
    expect(String(server.mock.calls[0]?.[0])).toContain('/merchant/auth/token-refresh')
  })
})
