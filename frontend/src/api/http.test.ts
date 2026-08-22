import { describe, expect, it, vi } from 'vitest'

import { apiRequest, createIdempotencyKey } from '@/api/http'

describe('API client', () => {
  it('unwraps the standard response envelope and sends credentials', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: { ok: true }, meta: { request_id: 'req_test' } }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
    const result = await apiRequest<{ ok: boolean }>('/health')
    expect(result.data.ok).toBe(true)
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

  it('creates sufficiently long unique idempotency keys', () => {
    const first = createIdempotencyKey('test')
    const second = createIdempotencyKey('test')
    expect(first.length).toBeGreaterThanOrEqual(16)
    expect(first).not.toBe(second)
  })
})
