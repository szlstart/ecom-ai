import { afterEach, describe, expect, it, vi } from 'vitest'

import { liveTraceFromEvent, RealtimeConnection, updateLiveTrace } from '@/api/realtime'

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
  it('still attempts a real connection when navigator.onLine is a stale false hint', async () => {
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.spyOn(window.navigator, 'onLine', 'get').mockReturnValue(false)
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        data: { ticket: 'rt_stale_offline', expires_in: 30, websocket_path: '/ws/v1', subprotocol: 'ecom.realtime.v1' },
        meta: { request_id: 'req_stale_offline', pagination: null },
      }), { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    const connection = new RealtimeConnection({
      audience: 'user', token: () => 'access-secret', onState: () => undefined, onEvent: () => undefined,
    })

    connection.start()
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    await vi.waitFor(() => expect(MockWebSocket.instances).toHaveLength(1))
    connection.stop()
  })

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
    await vi.waitFor(() => expect(events).toEqual(['rte_once']))
    connection.stop()
  })

  it('serializes async handlers so websocket frames cannot overtake each other', async () => {
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        data: { ticket: 'rt_secret', expires_in: 30, websocket_path: '/ws/v1', subprotocol: 'ecom.realtime.v1' },
        meta: { request_id: 'req_test', pagination: null },
      }), { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    let releaseFirst: (() => void) | undefined
    const firstPending = new Promise<void>((resolve) => { releaseFirst = resolve })
    const handled: string[] = []
    const connection = new RealtimeConnection({
      audience: 'user', token: () => 'token', onState: () => undefined,
      onEvent: async (event) => {
        handled.push(`start:${event.event_id}`)
        if (event.event_id === 'rte_1') await firstPending
        handled.push(`end:${event.event_id}`)
      },
    })
    connection.start()
    await vi.waitFor(() => expect(MockWebSocket.instances).toHaveLength(1))
    const socket = MockWebSocket.instances[0]!
    socket.open()
    socket.message({ schema_version: 1, event_id: 'rte_1', type: 'message.created', occurred_at: '', data: {} })
    socket.message({ schema_version: 1, event_id: 'rte_2', type: 'message.created', occurred_at: '', data: {} })
    await vi.waitFor(() => expect(handled).toEqual(['start:rte_1']))
    releaseFirst?.()
    await vi.waitFor(() => expect(handled).toEqual(['start:rte_1', 'end:rte_1', 'start:rte_2', 'end:rte_2']))
    connection.stop()
  })

  it('drops the old websocket and reconnects when the active account changes', async () => {
    vi.stubGlobal('WebSocket', MockWebSocket)
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify({
      data: {
        ticket: `rt_${MockWebSocket.instances.length + 1}`,
        expires_in: 30,
        websocket_path: '/ws/v1',
        subprotocol: 'ecom.realtime.v1',
      },
      meta: { request_id: 'req_identity_switch', pagination: null },
    }), { status: 200, headers: { 'content-type': 'application/json' } }))
    let token = 'account-a-token'
    const connection = new RealtimeConnection({
      audience: 'user', token: () => token, onState: () => undefined, onEvent: () => undefined,
    })

    connection.start()
    await vi.waitFor(() => expect(MockWebSocket.instances).toHaveLength(1))
    const first = MockWebSocket.instances[0]!
    first.open()
    token = 'account-b-token'

    await vi.waitFor(() => expect(MockWebSocket.instances).toHaveLength(2), { timeout: 1_500 })
    expect(first.readyState).toBe(3)
    connection.stop()
  })

  it('does not relabel an in-flight old-account ticket as the new account', async () => {
    vi.stubGlobal('WebSocket', MockWebSocket)
    let releaseFirstTicket: ((response: Response) => void) | undefined
    const firstTicket = new Promise<Response>((resolve) => { releaseFirstTicket = resolve })
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockImplementationOnce(async () => firstTicket)
      .mockResolvedValue(new Response(JSON.stringify({
        data: { ticket: 'rt_account_b', expires_in: 30, websocket_path: '/ws/v1', subprotocol: 'ecom.realtime.v1' },
        meta: { request_id: 'req_account_b', pagination: null },
      }), { status: 200, headers: { 'content-type': 'application/json' } }))
    let token = 'account-a-token'
    const connection = new RealtimeConnection({
      audience: 'user', token: () => token, onState: () => undefined, onEvent: () => undefined,
    })

    connection.start()
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    token = 'account-b-token'
    releaseFirstTicket?.(new Response(JSON.stringify({
      data: { ticket: 'rt_account_a', expires_in: 30, websocket_path: '/ws/v1', subprotocol: 'ecom.realtime.v1' },
      meta: { request_id: 'req_account_a', pagination: null },
    }), { status: 200, headers: { 'content-type': 'application/json' } }))

    await vi.waitFor(() => expect(MockWebSocket.instances).toHaveLength(2), { timeout: 1_500 })
    expect(MockWebSocket.instances[0]!.protocols).toContain('ticket.rt_account_a')
    expect(MockWebSocket.instances[0]!.readyState).toBe(3)
    expect(MockWebSocket.instances[1]!.protocols).toContain('ticket.rt_account_b')
    connection.stop()
  })

  it('ignores duplicate and out-of-order reasoning chunks', () => {
    const started = liveTraceFromEvent({
      schema_version: 1, event_id: 'rte_start', type: 'agent.response.started', occurred_at: '',
      data: { run_id: 'run_1' },
    })
    const latest = updateLiveTrace(started, {
      schema_version: 1, event_id: 'rte_2', type: 'agent.response.reasoning.delta', occurred_at: '',
      data: { run_id: 'run_1', chunk_index: 2, text_so_far: '最新推理' },
    })
    const stale = updateLiveTrace(latest, {
      schema_version: 1, event_id: 'rte_1', type: 'agent.response.reasoning.delta', occurred_at: '',
      data: { run_id: 'run_1', chunk_index: 1, text_so_far: '旧推理' },
    })
    expect(stale).toEqual(latest)
    expect(stale?.reasoning).toBe('最新推理')
  })
})
