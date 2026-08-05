import { api, getToken, ApiError } from './client'

export type TrackerPerms = {
  user_id: string
  resource_key: string
  can_view: boolean
  can_edit: boolean
  can_upload: boolean
  can_admin: boolean
}

export type TrackerRow = {
  row_id: string
  payment_id: string
  month_date: string | null
  txn_date: string | null
  amount: number | string | null
  eft_1: string | null
  eft_2: string | null
  transaction_type: string | null
  description: string | null
  check_reference: string | null
  bank_name: string | null
  billing_status: string | null
  collector: string | null
  posted: boolean | null
  notes: string | null
  assigned_date: string | null
  claims: string | null
  version: number
  deleted_at: string | null
}

export type UploadPreview = {
  preview_id: string
  expires_at: string
  filename?: string
  parsed_rows: number
  error_count: number
  errors_sample: Array<{ sheet: string; row?: number | null; message: string }>
  skipped_sheets: string[]
  counts: {
    adds: number
    updates: number
    unchanged: number
    soft_deletes: number
  }
  month_bounds: Array<{ from: string; to: string }>
  sample_adds: Array<{ payment_id: string }>
  sample_updates: Array<{ payment_id?: string; row_id: string }>
  sample_soft_deletes: Array<{ payment_id: string; row_id: string }>
}

export type TrackerGrant = {
  grant_id?: string
  user_id: string
  username?: string
  display_name?: string
  can_view: boolean
  can_edit: boolean
  can_upload: boolean
  can_admin: boolean
}

export async function fetchTrackerMe() {
  return api<TrackerPerms>('/api/tracker/me')
}

export async function fetchTrackerMonths() {
  return api<{ months: string[] }>('/api/tracker/months')
}

export async function fetchTrackerRows(params: {
  month?: string
  q?: string
  page?: number
  page_size?: number
  include_deleted?: boolean
}) {
  const sp = new URLSearchParams()
  if (params.month) sp.set('month', params.month)
  if (params.q) sp.set('q', params.q)
  if (params.page) sp.set('page', String(params.page))
  if (params.page_size) sp.set('page_size', String(params.page_size))
  if (params.include_deleted) sp.set('include_deleted', 'true')
  return api<{
    items: TrackerRow[]
    page: number
    page_size: number
    total: number
  }>(`/api/tracker/rows?${sp}`)
}

export async function createTrackerRow(body: Partial<TrackerRow>) {
  return api<TrackerRow>('/api/tracker/rows', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export async function patchTrackerRow(rowId: string, body: Record<string, unknown>) {
  return api<TrackerRow>(`/api/tracker/rows/${rowId}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

export async function softDeleteTrackerRow(rowId: string, version: number) {
  return api<TrackerRow>(`/api/tracker/rows/${rowId}/delete`, {
    method: 'POST',
    body: JSON.stringify({ version }),
  })
}

export async function restoreTrackerRow(rowId: string, version: number) {
  return api<TrackerRow>(`/api/tracker/rows/${rowId}/restore`, {
    method: 'POST',
    body: JSON.stringify({ version }),
  })
}

export async function fetchRowHistory(rowId: string) {
  return api<{
    items: Array<{
      audit_id: string
      action: string
      acted_at: string
      actor_display_name?: string
      actor_username?: string
      before_json?: Record<string, unknown> | null
      after_json?: Record<string, unknown> | null
    }>
  }>(`/api/tracker/rows/${rowId}/history`)
}

export async function downloadTrackerExport(month?: string) {
  const sp = new URLSearchParams()
  if (month) {
    const [y, m] = month.split('-').map(Number)
    const from = `${month}-01`
    const last = new Date(y, m, 0).getDate()
    const to = `${month}-${String(last).padStart(2, '0')}`
    sp.set('from', from)
    sp.set('to', to)
  }
  const token = getToken()
  const res = await fetch(`/api/tracker/export?${sp}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!res.ok) {
    let detail: unknown = res.statusText
    try {
      detail = (await res.json()).detail
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail)
  }
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'Transaction_Tracker.xlsx'
  a.click()
  URL.revokeObjectURL(url)
}

export async function previewTrackerUpload(file: File) {
  const fd = new FormData()
  fd.append('file', file)
  return api<UploadPreview>('/api/tracker/upload/preview', {
    method: 'POST',
    body: fd,
  })
}

export async function commitTrackerUpload(previewId: string) {
  return api<{ ok: boolean; adds: number; updates: number; soft_deletes: number }>(
    '/api/tracker/upload/commit',
    {
      method: 'POST',
      body: JSON.stringify({ preview_id: previewId }),
    },
  )
}

export async function fetchTrackerGrants() {
  return api<{
    grants: TrackerGrant[]
    users: Array<{
      user_id: string
      username: string
      display_name: string
      is_active: boolean
      roles: string[]
    }>
  }>('/api/tracker/grants')
}

export async function putTrackerGrant(
  userId: string,
  body: {
    can_view: boolean
    can_edit: boolean
    can_upload: boolean
    can_admin: boolean
  },
) {
  return api(`/api/tracker/grants/${userId}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

export async function deleteTrackerGrant(userId: string) {
  return api(`/api/tracker/grants/${userId}`, { method: 'DELETE' })
}
