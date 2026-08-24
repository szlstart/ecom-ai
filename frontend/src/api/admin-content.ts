import { apiRequest, type ApiResult } from '@/api/http'

export interface ContentVersion { content_version_id: string; version: string; locale: string; region_code: string; format: string; blocks: Array<Record<string, unknown>> | null; html: string | null; text: string; status: string; effective_at: string; expires_at: string | null }
export interface PlatformContent { content_id: string; content_key: string; content_type: string; title: string; status: string; version: number; versions: ContentVersion[] }
export interface ContentInput { title: string; locale: string; region_code: string; source_format: 'plain_text' | 'structured' | 'html'; source_content: string }
export function listContent(token: string): Promise<ApiResult<{ items: PlatformContent[] }>> { return apiRequest('/admin/content', {}, token) }
export function getContent(id: string, token: string): Promise<ApiResult<PlatformContent>> { return apiRequest(`/admin/content/${encodeURIComponent(id)}`, {}, token) }
export function createContent(payload: ContentInput & { content_key: string; content_type: 'banner' | 'announcement' | 'help_article' | 'footer' | 'about' }, token: string): Promise<ApiResult<PlatformContent>> { return apiRequest('/admin/content', { method: 'POST', body: JSON.stringify(payload) }, token) }
export function updateContent(id: string, payload: ContentInput, version: number, token: string): Promise<ApiResult<PlatformContent>> { return apiRequest(`/admin/content/${encodeURIComponent(id)}`, { method: 'PUT', headers: { 'If-Match': `"${version}"` }, body: JSON.stringify(payload) }, token) }
export function publishContent(id: string, version: string, token: string): Promise<ApiResult<PlatformContent>> { return apiRequest(`/admin/content/${encodeURIComponent(id)}/versions/${encodeURIComponent(version)}/publish`, { method: 'POST' }, token) }
export function withdrawContent(id: string, token: string): Promise<ApiResult<PlatformContent>> { return apiRequest(`/admin/content/${encodeURIComponent(id)}/withdraw`, { method: 'POST' }, token) }
