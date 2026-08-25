import { apiRequest, createIdempotencyKey, type ApiResult } from '@/api/http'

export interface KnowledgeDocument {
  document_id: string
  scope_type: 'platform' | 'store'
  scope_id: string
  title: string
  content_version: string
  status: string
  index_job_no: string | null
  index_status: string | null
}

export interface AgentVersionSummary {
  version_no: number
  status: string
  model_profile: string
  tool_allowlist: string[]
  system_prompt: string
  policy_config: Record<string, unknown>
}

export interface AgentSummary {
  agent_id: string
  agent_code: string
  display_name: string
  scope_type: string
  status: string
  versions: AgentVersionSummary[]
}

export interface SkillSummary {
  skill_id: string
  skill_code: string
  display_name: string
  status: string
  latest_version: number | null
  published_version: number | null
}

export interface ToolSummary {
  tool_code: string
  server_code: string
  risk_level: string
  input_schema: Record<string, unknown> | null
  output_schema: Record<string, unknown> | null
  status: string
  latest_version: number | null
  published_version: number | null
}

export interface McpServerSummary {
  server_code: string
  tools: string[]
  timeout_seconds: number
}

export interface KillSwitch {
  switch_id: string
  target_type: string
  target_code: string
  is_active: boolean
  reason: string | null
  version: number
}

export interface KnowledgeIndexJob {
  job_id: string
  command_job_id: string
  status: string
  progress: number
  error_code: string | null
}

export interface ApprovalRequired {
  command_status: 'approval_required'
  approval_request_id: string
  required_approval_count: number
  approved_count: number
  expires_at: string
}

function get<T>(path: string, token: string): Promise<ApiResult<T>> {
  return apiRequest(path, {}, token)
}

function post<T>(path: string, payload: unknown, token: string): Promise<ApiResult<T>> {
  return apiRequest(path, { method: 'POST', body: JSON.stringify(payload) }, token)
}

export const listKnowledgeDocuments = (token: string) =>
  get<{ items: KnowledgeDocument[] }>('/admin/knowledge/documents', token)
export const getKnowledgeDocument = (id: string, token: string) =>
  get<KnowledgeDocument>(`/admin/knowledge/documents/${encodeURIComponent(id)}`, token)
export const createKnowledgeDocument = (
  payload: { scope_type: string; scope_id: string; title: string; safe_text: string },
  token: string,
) => post<KnowledgeDocument>('/admin/knowledge/documents', payload, token)
export const publishKnowledgeDocument = (id: string, token: string) =>
  apiRequest<KnowledgeDocument>(
    `/admin/knowledge/documents/${encodeURIComponent(id)}/publications`,
    { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('knowledge-index') }, body: '{}' },
    token,
  )
export const withdrawKnowledgeDocument = (id: string, token: string) =>
  apiRequest<KnowledgeDocument>(
    `/admin/knowledge/documents/${encodeURIComponent(id)}`,
    { method: 'DELETE' },
    token,
  )
export const getKnowledgeIndexJob = (id: string, token: string) =>
  get<KnowledgeIndexJob>(`/admin/knowledge/index-jobs/${encodeURIComponent(id)}`, token)
export const cancelKnowledgeIndexJob = (id: string, token: string) =>
  post<KnowledgeIndexJob>(
    `/admin/knowledge/index-jobs/${encodeURIComponent(id)}/cancellations`,
    {},
    token,
  )
export const listAgents = (token: string) => get<{ items: AgentSummary[] }>('/admin/ai/agents', token)
export const createAgentVersion = (id: string, payload: Record<string, unknown>, token: string) =>
  post<AgentSummary>(`/admin/ai/agents/${encodeURIComponent(id)}/versions`, payload, token)
export const bindAgentSkill = (
  id: string,
  version: number,
  payload: { skill_id: string; skill_version_no: number },
  token: string,
) => post<unknown>(
  `/admin/ai/agents/${encodeURIComponent(id)}/versions/${version}/skill-bindings`,
  payload,
  token,
)
export const publishAgentVersion = (id: string, version: number, token: string) =>
  apiRequest<ApprovalRequired>(
    `/admin/ai/agents/${encodeURIComponent(id)}/versions/${version}/publications`,
    { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('agent-publish') }, body: '{}' },
    token,
  )
export const listSkills = (token: string) => get<{ items: SkillSummary[] }>('/admin/ai/skills', token)
export const createSkill = (payload: { skill_code: string; display_name: string }, token: string) =>
  post<SkillSummary>('/admin/ai/skills', payload, token)
export const createSkillVersion = (
  id: string,
  payload: Record<string, unknown>,
  token: string,
) => post<SkillSummary>(`/admin/ai/skills/${encodeURIComponent(id)}/versions`, payload, token)
export const bindSkillTool = (
  id: string,
  version: number,
  payload: Record<string, unknown>,
  token: string,
) => post<unknown>(
  `/admin/ai/skills/${encodeURIComponent(id)}/versions/${version}/tool-bindings`,
  payload,
  token,
)
export const publishSkillVersion = (id: string, version: number, token: string) =>
  apiRequest<ApprovalRequired>(
    `/admin/ai/skills/${encodeURIComponent(id)}/versions/${version}/publications`,
    { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('skill-publish') }, body: '{}' },
    token,
  )
export const listTools = (token: string) => get<{ items: ToolSummary[] }>('/admin/ai/tools', token)
export const createTool = (
  payload: { tool_code: string; server_code: string; risk_level: string },
  token: string,
) => post<ToolSummary>('/admin/ai/tools', payload, token)
export const createToolVersion = (
  code: string,
  payload: Record<string, unknown>,
  token: string,
) => post<ToolSummary>(`/admin/ai/tools/${encodeURIComponent(code)}/versions`, payload, token)
export const publishToolVersion = (code: string, version: number, token: string) =>
  apiRequest<ApprovalRequired>(
    `/admin/ai/tools/${encodeURIComponent(code)}/versions/${version}/publications`,
    { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('tool-publish') }, body: '{}' },
    token,
  )
export const listMcpServers = (token: string) =>
  get<{ items: McpServerSummary[] }>('/admin/ai/mcp-servers', token)
export const listKillSwitches = (token: string) =>
  get<{ items: KillSwitch[] }>('/admin/ai/kill-switches', token)
export const changeKillSwitch = (
  targetType: string,
  targetCode: string,
  active: boolean,
  reason: string,
  token: string,
) => post<KillSwitch>(
  `/admin/ai/kill-switches/${encodeURIComponent(targetType)}/${encodeURIComponent(targetCode)}/${active ? 'activations' : 'deactivations'}`,
  { reason },
  token,
)
