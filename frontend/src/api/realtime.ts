import { API_BASE_URL, apiRequest } from '@/api/http'

export type RealtimeState = 'connected' | 'polling' | 'offline'

export interface RealtimeEvent {
  schema_version: number
  event_id: string
  type: string
  occurred_at: string
  data: Record<string, unknown>
}

export interface AgentLiveTrace {
  runId: string
  question: string
  stage: string
  label: string
  summary: string
}

export function liveTraceFromEvent(event: RealtimeEvent): AgentLiveTrace | null {
  const runId = event.data.run_id
  if (event.type !== 'agent.response.started' || typeof runId !== 'string') return null
  return {
    runId,
    question: String(event.data.question ?? ''),
    stage: String(event.data.stage ?? 'understanding'),
    label: String(event.data.label ?? '思考开始'),
    summary: String(event.data.summary ?? '正在理解问题并核对可用权限。'),
  }
}

interface RealtimeTicket {
  ticket: string
  expires_in: number
  websocket_path: string
  subprotocol: 'ecom.realtime.v1'
}

interface RealtimeOptions {
  audience: 'user' | 'admin'
  token: () => string
  onEvent: (event: RealtimeEvent) => void | Promise<void>
  onState: (state: RealtimeState) => void
  beforeReconnect?: () => void | Promise<void>
}

export class RealtimeConnection {
  private socket: WebSocket | null = null
  private stopped = false
  private retryTimer: number | undefined
  private attempt = 0
  private connecting = false
  private readonly seenEventIds = new Set<string>()

  constructor(private readonly options: RealtimeOptions) {}

  start(): void {
    this.stopped = false
    void this.connect(false)
  }

  stop(): void {
    this.stopped = true
    if (this.retryTimer) window.clearTimeout(this.retryTimer)
    this.retryTimer = undefined
    this.socket?.close(1000, 'page closed')
    this.socket = null
  }

  private async connect(reconnecting: boolean): Promise<void> {
    if (this.socket?.readyState === WebSocket.OPEN || this.socket?.readyState === WebSocket.CONNECTING) return
    if (this.stopped || this.connecting || !navigator.onLine) {
      this.options.onState(navigator.onLine ? 'polling' : 'offline')
      return
    }
    this.connecting = true
    this.options.onState('polling')
    try {
      if (reconnecting) await this.options.beforeReconnect?.()
      const path = this.options.audience === 'admin' ? '/support/realtime/tickets' : '/realtime/tickets'
      const ticket = (await apiRequest<RealtimeTicket>(path, { method: 'POST' }, this.options.token())).data
      if (this.stopped) return
      const socket = new WebSocket(resolveWebSocketUrl(ticket.websocket_path), [ticket.subprotocol, `ticket.${ticket.ticket}`])
      this.socket = socket
      socket.onopen = () => {
        this.attempt = 0
        this.options.onState('connected')
      }
      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(String(message.data)) as RealtimeEvent
          if (event.type === 'server.ping') {
            socket.send(JSON.stringify({ type: 'client.pong' }))
            return
          }
          if (this.seenEventIds.has(event.event_id)) return
          this.seenEventIds.add(event.event_id)
          if (this.seenEventIds.size > 2_000) this.seenEventIds.delete(this.seenEventIds.values().next().value!)
          void this.options.onEvent(event)
        } catch {
          socket.close(1008, 'invalid server frame')
        }
      }
      socket.onerror = () => this.options.onState(navigator.onLine ? 'polling' : 'offline')
      socket.onclose = () => {
        if (this.socket === socket) this.socket = null
        if (!this.stopped) this.scheduleReconnect()
      }
    } catch {
      if (!this.stopped) this.scheduleReconnect()
    } finally {
      this.connecting = false
    }
  }

  private scheduleReconnect(): void {
    this.options.onState(navigator.onLine ? 'polling' : 'offline')
    if (this.retryTimer || this.stopped) return
    const base = Math.min(30_000, 500 * 2 ** Math.min(this.attempt, 6))
    const delay = Math.round(base * (0.8 + Math.random() * 0.4))
    this.attempt += 1
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = undefined
      void this.connect(true)
    }, delay)
  }
}

function resolveWebSocketUrl(path: string): string {
  const configured = import.meta.env.VITE_WEBSOCKET_URL as string | undefined
  if (configured) return new URL(path, configured).toString()
  if (API_BASE_URL.startsWith('http://') || API_BASE_URL.startsWith('https://')) {
    const url = new URL(API_BASE_URL)
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
    url.pathname = path
    url.search = ''
    url.hash = ''
    return url.toString()
  }
  const url = new URL(path, window.location.origin)
  url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}
