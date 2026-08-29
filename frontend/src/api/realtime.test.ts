import { afterEach, describe, expect, it, vi } from 'vitest'

import { RealtimeConnection } from '@/api/realtime'

class MockWebSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly instances: MockWebSocket[] = []

  readonly url: string
  readonly protocols: string[]
  readyState = MockWebSocket.CONNECTING
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  sent: string[] = []

  constructor(url: string | URL, protocols: string | string[]) {
    this.url = String(url)
    this.protocols = Array.isArray(protocols) ? protocols : [protocols]
    MockWebSocket.instances.push(this)
  }

  send(value: string) { this.sent.push(value) }
  close() { this.readyState = 3 }
  open() { this.readyState = MockWebSocket.OPEN; this.onopen?.() }
  message(value: object) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(value) })) }
}

afterEach(() => { MockWebSocket.instances.length = 0; vi.unstubAllGlobals() })

describe('RealtimeConnection', () => {
  it('keeps the one-time ticket out of the URL and deduplicates event frames', async () => {
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        data: { ticket: 'rt_secret', expires_in: 30, websocket_path: '/ws/v1', subprotocol: 'ecom.realtime.v1' },
        meta: { request_id: 'req_test', pagination: null },
      }), { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    const states: string[] = []
    const events: string[] = []
    const connection = new RealtimeConnection({
      audience: 'user',
      token: () => 'access-secret',
      onState: (state) => states.push(state),
      onEvent: (event) => { events.push(event.event_id) },
    })

    connection.start()
    await vi.waitFor(() => expect(MockWebSocket.instances).toHaveLength(1))
    const socket = MockWebSocket.instances[0]!
    expect(socket.url).toBe('ws://127.0.0.1:8000/ws/v1')
    expect(socket.url).not.toContain('rt_secret')
    expect(socket.protocols).toEqual(['ecom.realtime.v1', 'ticket.rt_secret'])
    socket.open()
    expect(states).toContain('connected')

    socket.message({ schema_version: 1, event_id: 'rte_ping', type: 'server.ping', occurred_at: '', data: {} })
    expect(JSON.parse(socket.sent[0]!)).toEqual({ type: 'client.pong' })
    const event = { schema_version: 1, event_id: 'rte_once', type: 'message.created', occurred_at: '', data: {} }
    socket.message(event)
    socket.message(event)
    expect(events).toEqual(['rte_once'])
    connection.stop()
  })
})
