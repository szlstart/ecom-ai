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

export function listAgentConsents(token: string): Promise<ApiResult<{ items: AgentConsent[] }>> {
  return apiRequest('/users/me/agent-consents', {}, token)
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
  return apiRequest(`/users/me/agent-consents/${encodeURIComponent(consentId)}/revocations`, {
    method: 'POST',
  }, token)
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
