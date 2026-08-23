const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000/api/v1'

export function resolveApiAssetUrl(url: string | null): string | null {
  if (!url || !url.startsWith('/api/')) return url
  if (!API_BASE_URL.startsWith('http://') && !API_BASE_URL.startsWith('https://')) return url
  return new URL(url, API_BASE_URL).toString()
}

export interface PaginationMeta {
  previous_cursor: string | null
  next_cursor: string | null
  has_previous: boolean
  has_next: boolean
  limit: number
}

export interface ResponseMeta {
  request_id: string | null
  pagination: PaginationMeta | null
}

interface Envelope<T> {
  data: T
  meta: ResponseMeta
}

export interface ApiProblemBody {
  title: string
  status: number
  detail: string
  code: string
  request_id: string | null
  retryable: boolean
  errors?: Array<{ pointer: string; code: string; message: string }>
}

export class ApiProblem extends Error {
  readonly body: ApiProblemBody

  constructor(body: ApiProblemBody) {
    super(body.detail)
    this.name = 'ApiProblem'
    this.body = body
  }
}

export interface ApiResult<T> {
  data: T
  meta: ResponseMeta
  headers: Headers
  status: number
}

export async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
  accessToken?: string | null,
): Promise<ApiResult<T>> {
  const headers = new Headers(options.headers)
  if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
    credentials: 'include',
  })
  if (!response.ok) {
    const fallback: ApiProblemBody = {
      title: 'Request failed',
      status: response.status,
      detail: '请求失败，请稍后重试。',
      code: 'HTTP_REQUEST_FAILED',
      request_id: response.headers.get('x-request-id'),
      retryable: response.status >= 500,
    }
    const body = (await response.json().catch(() => fallback)) as ApiProblemBody
    throw new ApiProblem(body)
  }
  if (response.status === 204) {
    return {
      data: undefined as T,
      meta: { request_id: response.headers.get('x-request-id'), pagination: null },
      headers: response.headers,
      status: response.status,
    }
  }
  const body = (await response.json()) as Envelope<T>
  return { data: body.data, meta: body.meta, headers: response.headers, status: response.status }
}

export function createIdempotencyKey(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`
}

export function errorMessage(error: unknown): string {
  return error instanceof ApiProblem ? error.body.detail : '网络异常，请检查连接后重试。'
}

export async function downloadApiResource(
  path: string,
  accessToken: string,
  filename: string,
): Promise<void> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: { Authorization: `Bearer ${accessToken}` },
    credentials: 'include',
  })
  if (!response.ok) {
    const fallback: ApiProblemBody = {
      title: 'Download failed',
      status: response.status,
      detail: '文件下载失败，请稍后重试。',
      code: 'FILE_DOWNLOAD_FAILED',
      request_id: response.headers.get('x-request-id'),
      retryable: response.status >= 500,
    }
    throw new ApiProblem((await response.json().catch(() => fallback)) as ApiProblemBody)
  }
  const objectUrl = URL.createObjectURL(await response.blob())
  try {
    const anchor = document.createElement('a')
    anchor.href = objectUrl
    anchor.download = filename
    anchor.rel = 'noopener'
    anchor.click()
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0)
  }
}
