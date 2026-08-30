export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000/api/v1'

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

type UserAuthRecoveryHandler = (failedAccessToken: string) => Promise<string | null>
let userAuthRecoveryHandler: UserAuthRecoveryHandler | null = null
let managementAuthRecoveryHandler: UserAuthRecoveryHandler | null = null

export function registerUserAuthRecovery(handler: UserAuthRecoveryHandler | null): void {
  userAuthRecoveryHandler = handler
}

export function registerManagementAuthRecovery(handler: UserAuthRecoveryHandler | null): void {
  managementAuthRecoveryHandler = handler
}

export async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
  accessToken?: string | null,
): Promise<ApiResult<T>> {
  const headers = new Headers(options.headers)
  if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`)
  let response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
    credentials: 'include',
    // MySQL/Redis remain the authority for changing commerce data. Public API
    // responses may carry short CDN cache headers, but the SPA must not reuse a
    // browser memory/disk copy after a user clicks a filter, returns from an
    // editor, or another portal changes the same resource.
    cache: 'no-store',
  })
  if (
    response.status === 401
    && accessToken
    && (userAuthRecoveryHandler || managementAuthRecoveryHandler)
    && !path.startsWith('/auth/')
  ) {
    const recoveredToken = await userAuthRecoveryHandler?.(accessToken)
      ?? await managementAuthRecoveryHandler?.(accessToken)
    if (recoveredToken) {
      headers.set('Authorization', `Bearer ${recoveredToken}`)
      response = await fetch(`${API_BASE_URL}${path}`, {
        ...options,
        headers,
        credentials: 'include',
        cache: 'no-store',
      })
    }
  }
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

export function messageSendError(error: unknown): string {
  return error instanceof TypeError
    ? '服务连接暂时中断，消息内容已保留，请稍后重试。'
    : errorMessage(error)
}

export async function retryTransientNetworkRequest<T>(
  operation: () => Promise<T>,
  retryDelays: readonly number[] = [400, 900, 1_800, 3_600],
  onRetry?: () => void,
): Promise<T> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await operation()
    } catch (cause) {
      const delay = retryDelays[attempt]
      if (!(cause instanceof TypeError) || delay === undefined || !navigator.onLine) throw cause
      onRetry?.()
      await new Promise<void>((resolve) => window.setTimeout(resolve, delay))
    }
  }
}

export async function downloadApiResource(
  path: string,
  accessToken: string,
  filename: string,
): Promise<void> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: { Authorization: `Bearer ${accessToken}` },
    credentials: 'include',
    cache: 'no-store',
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
