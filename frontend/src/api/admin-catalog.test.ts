import { describe, expect, it, vi } from 'vitest'

import { adminCommand, adminReplace, versionEtag } from '@/api/admin-catalog'

describe('administration API helpers', () => {
  it('encodes strong resource versions exactly as the FastAPI contract expects', () => {
    expect(versionEtag(7)).toBe('"v7"')
  })

  it('adds If-Match to complete-set replacements', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: [], meta: { request_id: 'req_replace', pagination: null } }), { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    await adminReplace('/admin/products/prd_test/images', { items: [] }, 'admin-token', 4)
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit
    expect(init.method).toBe('PUT')
    expect(new Headers(init.headers).get('If-Match')).toBe('"v4"')
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer admin-token')
  })

  it('adds both version and idempotency protection to explicit commands', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ data: { ok: true }, meta: { request_id: 'req_command', pagination: null } }), { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    await adminCommand('/admin/products/prd_test/publications', { reason_code: 'READY', reason: '发布检查通过' }, 'admin-token', 9, 'product-publish')
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit
    const headers = new Headers(init.headers)
    expect(headers.get('If-Match')).toBe('"v9"')
    expect(headers.get('Idempotency-Key')).toMatch(/^product-publish-/)
  })
})
