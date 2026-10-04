import { api, ApiError } from './client'

export type ChecksPerms = {
  user_id: string
  resource_key: string
  can_view: boolean
  can_edit: boolean
  can_upload: boolean
  can_admin: boolean
}

export type ChecksRow = {
  row_id: string
  sheet_key: string
  check_date: string | null
  payer: string | null
  amount: number | string | null
  check_number: string | null
  deposit_date: string | null
  deposit_month: string | null
  link: string | null
  notes: string | null
  version: number
  deleted_at: string | null
}

export type ChecksUploadPreview = {
  preview_id: string
  expires_at: string
  filename?: string
  parsed_rows: number
  error_count: number
  errors_sample: Array<{ sheet: string; row?: number | null; message: string }>
  skipped_sheets: string[]
  skipped_out_of_year: number
  skipped_no_amount: number
  skipped_no_deposit_date: number
  duplicate_sheet_keys: number
  counts: {
    adds: number
    updates: number
    unchanged: number
    soft_deletes: number
  }
  month_bounds: Array<{ from: string; to: string }>
  sample_adds: Array<{ sheet_key: string }>
  sample_updates: Array<{ sheet_key?: string; row_id: string }>
  sample_soft_deletes: Array<{ sheet_key: string; row_id: string }>
}

export type ChecksGrant = {
  grant_id?: string
  user_id: string
  username?: string
  display_name?: string
  can_view: boolean
  can_edit: boolean
  can_upload: boolean
  can_admin: boolean
}

export async function fetchChecksMe() {
  return api<ChecksPerms>('/api/checks-deposits/me')
}

export async function fetchChecksMonths() {
  return api<{ months: string[] }>('/api/checks-deposits/months')
}

export async function fetchChecksRows(params: {
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
    items: ChecksRow[]
    page: number
    page_size: number
    total: number
    amount_total: number | string
  }>(`/api/checks-deposits/rows?${sp}`)
}

export async function createChecksRow(body: Partial<ChecksRow>) {
  return api<ChecksRow>('/api/checks-deposits/rows', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export async function patchChecksRow(rowId: string, body: Record<string, unknown>) {
  return api<ChecksRow>(`/api/checks-deposits/rows/${rowId}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

export async function softDeleteChecksRow(rowId: string, version: number) {
  return api<ChecksRow>(`/api/checks-deposits/rows/${rowId}/delete`, {
    method: 'POST',
    body: JSON.stringify({ version }),
  })
}

export async function restoreChecksRow(rowId: string, version: number) {
  return api<ChecksRow>(`/api/checks-deposits/rows/${rowId}/restore`, {
    method: 'POST',
    body: JSON.stringify({ version }),
  })
}

export async function fetchChecksRowHistory(rowId: string) {
  return api<{
    items: Array<{
      audit_id: string
      action: string
      acted_at: string
      actor_display_name?: string
      actor_username?: string
    }>
  }>(`/api/checks-deposits/rows/${rowId}/history`)
}

export async function downloadChecksExport(month?: string) {
  const sp = new URLSearchParams()
  if (month) {
    const [y, m] = month.split('-').map(Number)
    const from = `${month}-01`
    const last = new Date(y, m, 0).getDate()
    const to = `${month}-${String(last).padStart(2, '0')}`
    sp.set('from', from)
    sp.set('to', to)
  }
  const res = await fetch(`/api/checks-deposits/export?${sp}`, { credentials: 'include' })
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
  a.download = 'Checks_Deposits_2026.xlsx'
  a.click()
  URL.revokeObjectURL(url)
}

export async function previewChecksUpload(file: File) {
  const fd = new FormData()
  fd.append('file', file)
  return api<ChecksUploadPreview>('/api/checks-deposits/upload/preview', {
    method: 'POST',
    body: fd,
  })
}

export async function commitChecksUpload(previewId: string) {
  return api<{ ok: boolean; adds: number; updates: number; soft_deletes: number }>(
    '/api/checks-deposits/upload/commit',
    {
      method: 'POST',
      body: JSON.stringify({ preview_id: previewId }),
    },
  )
}

export async function fetchChecksGrants() {
  return api<{
    grants: ChecksGrant[]
    users: Array<{
      user_id: string
      username: string
      display_name: string
      is_active: boolean
      roles: string[]
    }>
  }>('/api/checks-deposits/grants')
}

export async function putChecksGrant(
  userId: string,
  body: {
    can_view: boolean
    can_edit: boolean
    can_upload: boolean
    can_admin: boolean
  },
) {
  return api(`/api/checks-deposits/grants/${userId}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  })
}

export async function deleteChecksGrant(userId: string) {
  return api(`/api/checks-deposits/grants/${userId}`, { method: 'DELETE' })
}
