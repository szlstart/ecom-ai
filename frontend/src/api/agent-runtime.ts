import { apiRequest, createIdempotencyKey, type ApiResult } from '@/api/http'

export interface AgentConsent {
  consent_id: string
  consent_type: 'personalization' | 'order_read' | 'after_sale_write' | string
  scope_type: 'user' | 'conversation' | 'store'
  scope_id: string | null
  policy_version: string
  status: 'active' | 'paused' | 'revoked'
  expires_at: string | null
  revoked_at: string | null
  created_at: string
  version: number
}

export interface AgentToolApproval {
  approval_id: string
  run_id: string
  conversation_id: string
  action_type: 'refund_submit'
  approval_status: 'pending' | 'approved' | 'rejected' | 'expired' | 'consumed'
  decision: 'approve' | 'reject' | null
  draft: Record<string, unknown>
  expires_at: string
  decided_at: string | null
  version: number
}

export interface AiMemoryItem {
  memory_id: string
  namespace: 'exclusive' | 'store'
  store_id: string | null
  memory_type: string
  memory_key: string
  value: string
  source_type: string
  consent_id: string | null
  status: 'candidate' | 'active' | 'superseded' | 'revoked' | 'expired' | 'deleted'
  expires_at: string | null
  created_at: string
  updated_at: string
  version: number
}

export interface AiCleanupTask {
  cleanup_task_id: string
  command_type: string
  scope_type: string
  scope_id: string
  source_resource_type: string
  source_resource_id: string
  status: 'queued' | 'running' | 'succeeded' | 'partial_failed' | 'failed'
  total_count: number
  processed_count: number
  failed_count: number
  retry_count: number
  max_retries: number
  error_code: string | null
  can_retry: boolean
  created_at: string
  updated_at: string
  version: number
}

export function listAgentConsents(token: string): Promise<ApiResult<{ items: AgentConsent[] }>> {
  return apiRequest('/users/me/agent-consents', {}, token)
}

export function listAiMemories(token: string): Promise<ApiResult<{ items: AiMemoryItem[] }>> {
  return apiRequest('/users/me/ai-memory-items', {}, token)
}

export function reviseAiMemory(
  memoryId: string, version: number, newValue: string, token: string,
): Promise<ApiResult<AiMemoryItem>> {
  return apiRequest(`/users/me/ai-memory-items/${encodeURIComponent(memoryId)}/revisions`, {
    method: 'POST',
    headers: { 'If-Match': `"v${version}"` },
    body: JSON.stringify({ new_value: newValue, reason_code: 'USER_CORRECTION', confirmed: true }),
  }, token)
}

export function activateAiMemory(
  memoryId: string, version: number, token: string,
): Promise<ApiResult<AiMemoryItem>> {
  return apiRequest(`/users/me/ai-memory-items/${encodeURIComponent(memoryId)}/activations`, {
    method: 'POST',
    headers: { 'If-Match': `"v${version}"` },
    body: JSON.stringify({ confirmed: true }),
  }, token)
}

export function deleteAiMemory(
  memoryId: string, version: number, token: string,
): Promise<ApiResult<AiCleanupTask>> {
  return apiRequest(`/users/me/ai-memory-items/${encodeURIComponent(memoryId)}`, {
    method: 'DELETE',
    headers: {
      'If-Match': `"v${version}"`,
      'Idempotency-Key': createIdempotencyKey('ai-memory-delete'),
    },
    body: JSON.stringify({ reason_code: 'USER_REQUEST', confirmed: true }),
  }, token)
}

export function disableAllAiPersonalization(token: string): Promise<ApiResult<AiCleanupTask>> {
  return apiRequest('/users/me/ai-personalization/disable-all', {
    method: 'POST',
    headers: { 'Idempotency-Key': createIdempotencyKey('ai-personalization-disable-all') },
    body: JSON.stringify({
      confirmation: 'DISABLE_ALL_AI_PERSONALIZATION',
      reason_code: 'USER_REQUEST',
    }),
  }, token)
}

export function getAiCleanupTask(
  taskId: string, token: string,
): Promise<ApiResult<AiCleanupTask>> {
  return apiRequest(`/users/me/ai-cleanup-tasks/${encodeURIComponent(taskId)}`, {}, token)
}

export function retryAiCleanupTask(
  taskId: string, version: number, token: string,
): Promise<ApiResult<AiCleanupTask>> {
  return apiRequest(`/users/me/ai-cleanup-tasks/${encodeURIComponent(taskId)}/retries`, {
    method: 'POST',
    headers: {
      'If-Match': `"v${version}"`,
      'Idempotency-Key': createIdempotencyKey('ai-cleanup-retry'),
    },
  }, token)
}

export function grantPersonalizationConsent(token: string): Promise<ApiResult<AgentConsent>> {
  return apiRequest('/users/me/agent-consents', {
    method: 'POST',
    headers: { 'Idempotency-Key': createIdempotencyKey('personalization-consent') },
    body: JSON.stringify({
      consent_type: 'personalization',
      scope_type: 'user',
      scope_id: null,
      policy_version: 'ai-personalization-v1',
      expires_at: null,
    }),
  }, token)
}

export function changeAgentConsent(
  consentId: string,
  command: 'pauses' | 'resumes' | 'revocations',
  token: string,
): Promise<ApiResult<AgentConsent>> {
  return apiRequest(`/users/me/agent-consents/${encodeURIComponent(consentId)}/${command}`, {
    method: 'POST',
  }, token)
}

export function grantAfterSaleAgentConsent(
  token: string,
  expiresAt: string,
): Promise<ApiResult<AgentConsent>> {
  return apiRequest('/users/me/agent-consents', {
    method: 'POST',
    headers: { 'Idempotency-Key': createIdempotencyKey('after-sale-consent') },
    body: JSON.stringify({
      consent_type: 'after_sale_write',
      scope_type: 'user',
      scope_id: null,
      policy_version: 'ai-after-sale-v1',
      expires_at: expiresAt,
    }),
  }, token)
}

export function revokeAgentConsent(
  consentId: string,
  token: string,
): Promise<ApiResult<AgentConsent>> {
  return changeAgentConsent(consentId, 'revocations', token)
}

export function getAgentToolApproval(
  approvalId: string,
  token: string,
): Promise<ApiResult<AgentToolApproval>> {
  return apiRequest(`/agent-tool-approvals/${encodeURIComponent(approvalId)}`, {}, token)
}

export function decideAgentToolApproval(
  approvalId: string,
  decision: 'approve' | 'reject',
  version: number,
  token: string,
): Promise<ApiResult<AgentToolApproval>> {
  return apiRequest(`/agent-tool-approvals/${encodeURIComponent(approvalId)}/decisions`, {
    method: 'POST',
    headers: {
      'If-Match': `"v${version}"`,
      'Idempotency-Key': createIdempotencyKey(`agent-${decision}`),
    },
    body: JSON.stringify({ decision }),
  }, token)
}
