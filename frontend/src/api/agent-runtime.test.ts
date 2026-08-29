import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  decideAgentToolApproval,
  grantAfterSaleAgentConsent,
} from '@/api/agent-runtime'

afterEach(() => vi.restoreAllMocks())

function response(data: Record<string, unknown>): Response {
  return new Response(JSON.stringify({ data, meta: { request_id: 'req_test', pagination: null } }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}

describe('agent runtime API client', () => {
  it('grants only the documented user-scoped after-sale consent', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response({ consent_id: 'con_1' }))
    await grantAfterSaleAgentConsent('token', '2026-09-24T00:00:00.000Z')

    const [, init] = fetchMock.mock.calls[0]!
    const headers = new Headers(init?.headers)
    expect(headers.get('Authorization')).toBe('Bearer token')
    expect(headers.get('Idempotency-Key')).toMatch(/^after-sale-consent-/)
    expect(JSON.parse(String(init?.body))).toEqual({
      consent_type: 'after_sale_write',
      scope_type: 'user',
      scope_id: null,
      policy_version: 'ai-after-sale-v1',
      expires_at: '2026-09-24T00:00:00.000Z',
    })
  })

  it('uses an explicit decision resource, version guard and idempotency key', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response({ approval_id: 'apr_1' }))
    await decideAgentToolApproval('apr_1', 'approve', 4, 'token')

    const [url, init] = fetchMock.mock.calls[0]!
    const headers = new Headers(init?.headers)
    expect(String(url)).toContain('/agent-tool-approvals/apr_1/decisions')
    expect(headers.get('If-Match')).toBe('"v4"')
    expect(headers.get('Idempotency-Key')).toMatch(/^agent-approve-/)
    expect(JSON.parse(String(init?.body))).toEqual({ decision: 'approve' })
  })
})
