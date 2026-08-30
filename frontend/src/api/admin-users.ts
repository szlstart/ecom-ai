import { apiRequest, createIdempotencyKey, type ApiResult } from '@/api/http'

export interface AdminUserSummary {
  user_id: string
  username: string
  nickname: string
  account_status: string
  registered_at: string
  last_login_at: string | null
  permission_version: number
  version: number
}

export interface AdminUserWorkspace {
  user_id: string
  username: string
  current_email: string | null
  presence_status: 'online' | 'offline' | 'frozen'
  balance_minor: string
  currency: string
  avatar_url: string | null
}

export function listAdminUsers(token: string): Promise<ApiResult<{ items: AdminUserSummary[]; next_cursor: string | null }>> {
  return apiRequest('/admin/users?limit=100', {}, token)
}

export function createAdminUser(
  payload: { username: string; password: string; email: string },
  token: string,
): Promise<ApiResult<AdminUserSummary>> {
  return apiRequest('/admin/users', {
    method: 'POST',
    headers: { 'Idempotency-Key': createIdempotencyKey('admin-user-create') },
    body: JSON.stringify(payload),
  }, token)
}

export function updateAdminUser(
  userId: string,
  payload: { username?: string; nickname?: string; email?: string; avatar_file_id?: string | null },
  version: number,
  token: string,
): Promise<ApiResult<AdminUserSummary>> {
  return apiRequest(`/admin/users/${encodeURIComponent(userId)}`, {
    method: 'PATCH',
    headers: { 'If-Match': `"v${version}"` },
    body: JSON.stringify(payload),
  }, token)
}

export function replaceAdminUserPassword(
  userId: string,
  payload: { temporary_password: string; require_change_on_next_login?: boolean },
  token: string,
): Promise<ApiResult<{ message: string }>> {
  return apiRequest(`/admin/users/${encodeURIComponent(userId)}/password-replacements`, {
    method: 'POST',
    headers: { 'Idempotency-Key': createIdempotencyKey('admin-user-password') },
    body: JSON.stringify(payload),
  }, token)
}

export function adjustAdminUserWallet(
  userId: string,
  payload: { direction: 'credit' | 'debit'; amount_minor: number },
  token: string,
): Promise<ApiResult<{ transaction_id: string; direction: string; amount_minor: string; balance_minor: string; currency: string }>> {
  return apiRequest(`/admin/users/${encodeURIComponent(userId)}/wallet-adjustments`, {
    method: 'POST',
    headers: { 'Idempotency-Key': createIdempotencyKey('admin-wallet-adjust') },
    body: JSON.stringify(payload),
  }, token)
}

export function deleteAdminUser(
  userId: string,
  version: number,
  token: string,
): Promise<ApiResult<void>> {
  return apiRequest(`/admin/users/${encodeURIComponent(userId)}`, {
    method: 'DELETE',
    headers: { 'If-Match': `"v${version}"` },
  }, token)
}


export function getAdminUserWorkspace(userId: string, token: string): Promise<ApiResult<AdminUserWorkspace>> {
  return apiRequest(`/admin/users/${encodeURIComponent(userId)}/workspace`, {}, token)
}
