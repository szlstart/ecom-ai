import { afterEach, describe, expect, it, vi } from 'vitest'

import { apiRequest, createIdempotencyKey, registerManagementAuthRecovery, registerUserAuthRecovery, resolveApiAssetUrl } from '@/api/http'

describe('API client', () => {
  afterEach(() => {
    registerUserAuthRecovery(null)
    registerManagementAuthRecovery(null)
  })

  it('unwraps the standard response envelope and sends credentials', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: { ok: true }, meta: { request_id: 'req_test', pagination: null } }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
    const result = await apiRequest<{ ok: boolean }>('/health')
    expect(result.data.ok).toBe(true)
    expect(result.meta).toEqual({ request_id: 'req_test', pagination: null })
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/health'),
      expect.objectContaining({ credentials: 'include' }),
    )
  })

  it('surfaces RFC problem details without leaking transport internals', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ title: 'Denied', status: 403, detail: '无权访问。', code: 'AUTH_PERMISSION_DENIED', request_id: 'req_test', retryable: false }),
        { status: 403, headers: { 'content-type': 'application/problem+json' } },
      ),
    )
    await expect(apiRequest('/admin/users')).rejects.toMatchObject({
      body: { code: 'AUTH_PERMISSION_DENIED' },
    })
  })

  it('recovers a rejected user token and retries the request exactly once', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(new Response(
        JSON.stringify({ data: { ok: true }, meta: { request_id: 'req_retried', pagination: null } }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      ))
    const recover = vi.fn().mockResolvedValue('fresh-user-token')
    registerUserAuthRecovery(recover)

    const result = await apiRequest<{ ok: boolean }>('/users/me', {}, 'expired-user-token')

    expect(result.data.ok).toBe(true)
    expect(recover).toHaveBeenCalledOnce()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(new Headers(fetchMock.mock.calls[1]?.[1]?.headers).get('Authorization')).toBe(
      'Bearer fresh-user-token',
    )
  })

  it('does not recursively recover authentication endpoints', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 401 }))
    const recover = vi.fn().mockResolvedValue('fresh-user-token')
    registerUserAuthRecovery(recover)

    await expect(apiRequest('/auth/token-refresh', {}, 'expired-user-token')).rejects.toMatchObject({
      body: { status: 401 },
    })
    expect(recover).not.toHaveBeenCalled()
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('creates sufficiently long unique idempotency keys', () => {
    const first = createIdempotencyKey('test')
    const second = createIdempotencyKey('test')
    expect(first.length).toBeGreaterThanOrEqual(16)
    expect(first).not.toBe(second)
  })

  it('resolves API-owned file URLs against the configured API origin', () => {
    expect(resolveApiAssetUrl('/api/v1/files/file_test/content')).toBe(
      'http://127.0.0.1:8000/api/v1/files/file_test/content',
    )
    expect(resolveApiAssetUrl('https://cdn.example.test/image.webp')).toBe(
      'https://cdn.example.test/image.webp',
    )
    expect(resolveApiAssetUrl(null)).toBeNull()
  })
})
