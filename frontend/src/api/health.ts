import { API_BASE_URL } from './http'

export interface DependencyHealth {
  status: 'up' | 'down' | 'degraded' | 'unknown' | 'skipped'
  required?: boolean
  code?: string | null
}

export interface ReadinessHealth {
  status: 'ready' | 'degraded' | 'not_ready'
  dependencies: Record<string, DependencyHealth>
}

function healthUrl(): string {
  if (API_BASE_URL.startsWith('http://') || API_BASE_URL.startsWith('https://')) {
    return new URL('/health/ready', API_BASE_URL).toString()
  }
  return '/health/ready'
}

export async function getReadinessHealth(): Promise<ReadinessHealth> {
  const response = await fetch(healthUrl(), {
    credentials: 'include',
    cache: 'no-store',
  })
  const payload = await response.json().catch(() => null) as ReadinessHealth | null
  // A 503 response still contains the useful dependency report. Only reject
  // malformed/transport failures so the UI can distinguish a real outage.
  if (!payload || typeof payload !== 'object' || !payload.dependencies) {
    throw new Error('健康检查响应无效')
  }
  return payload
}

export function resolveAgentHealth(
  health: ReadinessHealth | null,
): 'available' | 'degraded' | 'unavailable' | 'unknown' {
  if (!health) return 'unknown'
  const runtime = health.dependencies.agent_runtime?.status
  const model = health.dependencies.agent_model?.status
  if (runtime === 'down' || model === 'down') return 'unavailable'
  if (runtime === 'up' && model === 'up') return 'available'
  if ([runtime, model].some((status) => status === 'degraded' || status === 'unknown')) {
    return 'degraded'
  }
  return 'unknown'
}
