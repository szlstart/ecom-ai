import { apiRequest, createIdempotencyKey, type ApiResult } from '@/api/http'

export interface UploadPolicy {
  purpose: string
  allowed_mime_types: string[]
  allowed_extensions: string[]
  max_size_bytes: number
  max_count: number
}

interface FileVariant {
  file_id: string
  status: string
  scan_status: string
}

interface UploadSession {
  upload_id: string
  upload_status: string
  upload: { method: string; url: string; headers: Record<string, string>; expires_at: string } | null
  bindable_file: FileVariant | null
}

export type UploadProgress = (message: string) => void

export function getUploadPolicy(purpose: string): Promise<ApiResult<UploadPolicy>> {
  return apiRequest(`/file-upload-policies/${encodeURIComponent(purpose)}`)
}

export async function uploadBindableFile(
  file: File,
  purpose: string,
  accessToken: string,
  policy: UploadPolicy,
  onProgress: UploadProgress = () => undefined,
  businessContextId: string | null = null,
): Promise<string> {
  validateFile(file, policy)
  onProgress('正在计算文件校验值…')
  const sha256 = await digest(file)
  onProgress('正在创建受控上传会话…')
  const session = (await apiRequest<UploadSession>('/file-upload-sessions', {
    method: 'POST',
    headers: { 'Idempotency-Key': createIdempotencyKey('file-upload') },
    body: JSON.stringify({
      purpose,
      filename: file.name,
      size_bytes: file.size,
      content_type: file.type || 'application/octet-stream',
      sha256,
      business_context_id: businessContextId,
    }),
  }, accessToken)).data
  if (!session.upload) throw new Error('上传会话未返回对象存储指令')

  onProgress('正在上传到临时隔离区…')
  let uploadResponse: Response
  try {
    uploadResponse = await fetch(session.upload.url, {
      method: session.upload.method,
      headers: session.upload.headers,
      body: file,
    })
  } catch {
    throw new Error('无法连接图片存储服务。请刷新页面后重试；若仍失败，请确认本地文件服务已启动。')
  }
  if (!uploadResponse.ok) throw new Error(`对象存储上传失败（${uploadResponse.status}）`)

  onProgress('正在验证并提交安全扫描…')
  await apiRequest<UploadSession>(`/file-upload-sessions/${encodeURIComponent(session.upload_id)}/complete`, {
    method: 'POST',
    headers: { 'Idempotency-Key': createIdempotencyKey('file-complete') },
    body: JSON.stringify({ sha256, provider_checksum: uploadResponse.headers.get('etag') }),
  }, accessToken)
  return waitUntilBindable(session.upload_id, accessToken, onProgress)
}

export async function loadProtectedFileObjectUrl(path: string, accessToken: string): Promise<string> {
  const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000/api/v1'
  const response = await fetch(`${apiBaseUrl}${path}`, {
    headers: { Authorization: `Bearer ${accessToken}` },
    credentials: 'include',
    cache: 'no-store',
  })
  if (!response.ok) throw new Error(`图片读取失败（${response.status}）`)
  return URL.createObjectURL(await response.blob())
}

function validateFile(file: File, policy: UploadPolicy): void {
  if (file.size > policy.max_size_bytes) {
    throw new Error(`文件超过 ${Math.ceil(policy.max_size_bytes / 1024 / 1024)} MB 上限`)
  }
  if (file.type && !policy.allowed_mime_types.includes(file.type)) {
    throw new Error(`不支持 ${file.type} 格式的文件`)
  }
}

async function waitUntilBindable(uploadId: string, accessToken: string, onProgress: UploadProgress): Promise<string> {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const current = (await apiRequest<UploadSession>(
      `/file-upload-sessions/${encodeURIComponent(uploadId)}`,
      {},
      accessToken,
    )).data
    if (current.bindable_file?.status === 'active' && current.bindable_file.scan_status === 'safe') {
      return current.bindable_file.file_id
    }
    if (current.bindable_file?.scan_status === 'rejected') throw new Error('文件未通过安全扫描，请更换文件。')
    onProgress(`安全扫描处理中（${attempt + 1}/30）…`)
    await new Promise((resolve) => window.setTimeout(resolve, 1000))
  }
  throw new Error('安全扫描仍在进行。未绑定文件会按生命周期规则自动清理，请稍后重试上传。')
}

async function digest(file: File): Promise<string> {
  const hash = await crypto.subtle.digest('SHA-256', await file.arrayBuffer())
  return Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, '0')).join('')
}
