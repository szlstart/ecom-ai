import { afterEach, describe, expect, it, vi } from 'vitest'

import { changeKillSwitch, publishAgentVersion, publishKnowledgeDocument } from '@/api/admin-ai'

afterEach(() => vi.restoreAllMocks())

function response(data: Record<string, unknown>): Response {
  return new Response(JSON.stringify({ data, meta: { request_id: 'req_ai', pagination: null } }), {
    status: 202,
    headers: { 'content-type': 'application/json' },
  })
}

describe('AI administration API client', () => {
  it('uses idempotency keys for index rebuild and publication approval commands', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => response({}))
    await publishKnowledgeDocument('kdoc_1', 'admin-token')
    await publishAgentVersion('agt_1', 3, 'admin-token')
    const first = new Headers(fetchMock.mock.calls[0]?.[1]?.headers)
    const second = new Headers(fetchMock.mock.calls[1]?.[1]?.headers)
    expect(first.get('Idempotency-Key')).toMatch(/^knowledge-index-/)
    expect(second.get('Idempotency-Key')).toMatch(/^agent-publish-/)
  })

  it('sends kill switch changes as explicit activation resources', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response({}))
    await changeKillSwitch('tool', 'catalog.get_product', true, '安全回滚', 'admin-token')
    const [url, init] = fetchMock.mock.calls[0]!
    expect(String(url)).toContain('/kill-switches/tool/catalog.get_product/activations')
    expect(JSON.parse(String(init?.body))).toEqual({ reason: '安全回滚' })
  })
})
