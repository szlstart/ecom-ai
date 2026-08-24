import { apiRequest, type ApiResult } from '@/api/http'

export interface PublishedContentVersion { content_version_id: string; version: string; locale: string; region_code: string; format: string; blocks: Array<Record<string, unknown>> | null; html: string | null; text: string; status: string; effective_at: string; expires_at: string | null }
export interface PublishedContent { content_id: string; content_key: string; content_type: string; title: string; version: PublishedContentVersion }
export function listHelp(): Promise<ApiResult<{ items: PublishedContent[] }>> { return apiRequest('/content/help-articles') }
export function getHelp(key: string): Promise<ApiResult<PublishedContent>> { return apiRequest(`/content/help-articles/${encodeURIComponent(key)}`) }
export function getAbout(): Promise<ApiResult<{ items: PublishedContent[] }>> { return apiRequest('/content/about') }
export function getFooter(): Promise<ApiResult<{ items: PublishedContent[] }>> { return apiRequest('/content/footer') }
