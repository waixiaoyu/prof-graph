/**
 * API client：统一相对路径（开发期由 Vite 代理到 FastAPI，
 * 生产期可用 VITE_API_BASE 指定后端地址）。
 */
const BASE: string = import.meta.env.VITE_API_BASE ?? ''

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`
    try {
      const body = (await resp.json()) as { detail?: string }
      if (body?.detail) detail = String(body.detail)
    } catch {
      /* 非 JSON 响应体，保留默认信息 */
    }
    throw new ApiError(resp.status, detail)
  }
  return (await resp.json()) as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  /** DELETE 带请求体（管理删除需 reason） */
  del: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'DELETE', body: JSON.stringify(body) }),
}
