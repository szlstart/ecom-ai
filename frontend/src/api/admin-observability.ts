import { apiRequest, createIdempotencyKey, type ApiResult } from '@/api/http'

export interface EvaluationRun {
  evaluation_id: string
  dataset_id: string
  dataset_version: string
  dataset_sha256: string
  baseline_type: string
  baseline_version: string
  candidate_type: string
  candidate_version: string
  require_significant_gain: boolean
  status: string
  release_gate: string | null
  report: Record<string, unknown> | null
  trace_id: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  error_code: string | null
}

export function listEvaluations(token: string): Promise<ApiResult<{ items: EvaluationRun[] }>> {
  return apiRequest('/admin/ai/evaluations', {}, token)
}

export function runEvaluation(payload: {
  dataset_id: 'ecom-ai-release-holdout'
  dataset_version: '2026.08.25-v1'
  baseline_type: string
  baseline_version: string
  candidate_type: string
  candidate_version: string
  require_significant_gain: boolean
}, token: string): Promise<ApiResult<EvaluationRun>> {
  return apiRequest('/admin/ai/evaluations', { method: 'POST', body: JSON.stringify(payload) }, token)
}

export interface ObservabilitySummary {
  window: 'process_lifetime'
  metrics: Record<string, string | number | boolean | null>
  trace_backend: string
  log_backend: string
  sensitive_content_included: false
}

export function getObservabilitySummary(token: string): Promise<ApiResult<ObservabilitySummary>> {
  return apiRequest('/admin/observability', {}, token)
}

export interface AdminAgentRun {
  run_id: string
  status: 'queued' | 'running' | 'waiting' | 'completed' | 'failed' | 'cancelled'
  current_phase: string
  agent_code: string
  agent_version_no: number
  conversation_type: 'exclusive' | 'store'
  trace_id: string
  context_ref_count: number
  error_code: string | null
  degraded_reason: string | null
  available_actions: Array<'cancel'>
  created_at: string
  updated_at: string
  version: number
}

export function getAdminAgentRun(id: string, token: string): Promise<ApiResult<AdminAgentRun>> {
  return apiRequest(`/admin/ai/runs/${encodeURIComponent(id)}`, {}, token)
}

export function cancelAdminAgentRun(
  id: string,
  reason: string,
  etag: string,
  token: string,
): Promise<ApiResult<AdminAgentRun>> {
  return apiRequest(
    `/admin/ai/runs/${encodeURIComponent(id)}/cancellations`,
    {
      method: 'POST',
      headers: {
        'Idempotency-Key': createIdempotencyKey('agent-run-cancel'),
        'If-Match': etag,
      },
      body: JSON.stringify({ reason }),
    },
    token,
  )
}
