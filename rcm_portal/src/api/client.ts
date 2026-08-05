const TOKEN_KEY = 'rcm_access_token'

export type Role = 'super_admin' | 'finance' | 'posting_team'

export type User = {
  user_id: string
  username: string
  email?: string | null
  display_name: string
  is_active: boolean
  roles: Role[]
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : JSON.stringify(detail))
    this.status = status
    this.detail = detail
  }
}

export async function api<T = unknown>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers || {})
  if (!headers.has('Content-Type') && init.body && !(init.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json')
  }
  const token = getToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const res = await fetch(path, { ...init, headers })
  if (!res.ok) {
    let detail: unknown = res.statusText
    try {
      const j = await res.json()
      detail = j.detail ?? j
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail)
  }
  if (res.status === 204) return undefined as T
  const ct = res.headers.get('content-type') || ''
  if (ct.includes('application/json')) return res.json()
  return res as unknown as T
}

export async function login(username: string, password: string) {
  const data = await api<{ access_token: string; user: User }>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
  setToken(data.access_token)
  return data.user
}

export async function fetchMe() {
  return api<User>('/api/auth/me')
}

export function money(n: number | null | undefined) {
  if (n == null || Number.isNaN(Number(n))) return '—'
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 0,
  }).format(Number(n))
}
