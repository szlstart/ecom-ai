import { apiRequest, createIdempotencyKey, type ApiResult } from '@/api/http'

export interface DeadLetterEvent {
  dead_letter_id: string
  source_type: string
  source_id: string
  event_type: string
  schema_version: number
  scope_type: string
  scope_id: number
  payload_hash: string
  payload_keys: string[]
  failure_count: number
  first_failed_at: string
  last_failed_at: string
  last_error_code: string
  last_error: string
  status: 'open' | 'replaying' | 'resolved' | 'ignored'
  replay_count: number
  last_replay_at: string | null
  original_trace_id: string | null
  replay_trace_id: string | null
  available_actions: Array<'preview_replay'>
  version: number
}
export interface DeadLetterReplayPreview {
  dead_letter: DeadLetterEvent
  replayable: boolean
  blockers: string[]
  source_status: string | null
  immutable_payload_hash: string
  impact_summary: string[]
  required_approval_count: number
  preview_token: string
  expires_at: string
}
export function listAdminDeadLetters(filters: Record<string, string>, token: string): Promise<ApiResult<{ items: DeadLetterEvent[] }>> { const query = new URLSearchParams(Object.entries(filters).filter(([, value]) => value)); query.set('limit', '100'); return apiRequest(`/admin/dead-letter-events?${query.toString()}`, {}, token) }
export function getAdminDeadLetter(id: string, token: string): Promise<ApiResult<DeadLetterEvent>> { return apiRequest(`/admin/dead-letter-events/${encodeURIComponent(id)}`, {}, token) }
export function previewAdminDeadLetterReplay(id: string, token: string): Promise<ApiResult<DeadLetterReplayPreview>> { return apiRequest(`/admin/dead-letter-events/${encodeURIComponent(id)}/replay-previews`, { method: 'POST' }, token) }
export function replayAdminDeadLetter(id: string, etag: string, previewToken: string, reasonCode: string, reason: string, token: string): Promise<ApiResult<{ command_status: 'approval_required'; approval_request_id: string; required_approval_count: number; approved_count: number; expires_at: string }>> { return apiRequest(`/admin/dead-letter-events/${encodeURIComponent(id)}/replays`, { method: 'POST', headers: { 'If-Match': etag, 'Idempotency-Key': createIdempotencyKey('admin-dead-letter-replay') }, body: JSON.stringify({ preview_token: previewToken, reason_code: reasonCode, reason }) }, token) }
