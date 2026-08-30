import { afterEach, describe, expect, it, vi } from 'vitest'

import { sendTextResilient } from '@/api/messaging'

function successfulMessageResponse() {
  return new Response(JSON.stringify({
    data: {
      message_id: 'msg_1',
      sequence_no: 1,
      sender_type: 'user',
      message_type: 'text',
      text: '你好',
      message_status: 'sent',
      moderation_status: 'passed',
      content: { type: 'text', text: '你好' },
      viewer_reaction: null,
      sent_at: '2026-08-30T13:00:00Z',
    },
    meta: { request_id: 'req_1', pagination: null },
  }), { status: 200, headers: { 'content-type': 'application/json' } })
}

describe('messaging API client', () => {
  afterEach(() => vi.restoreAllMocks())

  it('retries a transient network failure with the same client message id', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(successfulMessageResponse())
    const onRetry = vi.fn()

    const result = await sendTextResilient(
      'conv_1',
      '你好',
      'token',
      'cmsg_STABLE',
      [0],
      onRetry,
    )

    expect(result.data.message_id).toBe('msg_1')
    expect(fetchMock).toHaveBeenCalledTimes(2)
    const firstBody = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))
    const secondBody = JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))
    expect(firstBody.client_message_id).toBe('cmsg_STABLE')
    expect(secondBody.client_message_id).toBe('cmsg_STABLE')
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it('does not retry an HTTP business error', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      title: 'Invalid message',
      status: 422,
      detail: '消息内容不符合要求。',
      code: 'MESSAGE_INVALID',
      request_id: 'req_2',
      retryable: false,
    }), { status: 422, headers: { 'content-type': 'application/problem+json' } }))

    await expect(sendTextResilient('conv_1', '你好', 'token', 'cmsg_1', [0])).rejects.toMatchObject({
      body: { code: 'MESSAGE_INVALID' },
    })
    expect(fetchMock).toHaveBeenCalledOnce()
  })
})
