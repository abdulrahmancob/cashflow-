import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, ApiError, getToken } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import {
  Badge,
  Button,
  Card,
  Drawer,
  Input,
  KpiCard,
  Select,
  TextArea,
  Toast,
  statusTone,
} from '../components/ui'

type WorkItem = {
  work_item_id: string
  patient_name?: string
  emr_patient_id: string
  dos: string
  dob?: string
  facility_name: string
  insurance_name?: string
  eligibility_status: string
  reference_number?: string
  notes?: string
  assigned_to?: string
  assigned_to_name?: string
  updated_by_name?: string
  updated_at?: string
  locked_by?: string
  locked_by_name?: string
  context?: Record<string, unknown>
}

type Meta = {
  statuses: Array<{ status_key: string; display_name: string }>
  reasons: Array<{ reason_key: string; display_name: string; requires_text: boolean }>
  filters: {
    facility: string[]
    insurance: string[]
    month: string[]
    assignees: Array<{ user_id: string; display_name: string }>
    status: string[]
  }
}

function qs(params: Record<string, string | string[] | number | boolean | undefined>) {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v == null || v === '' || v === false) continue
    if (Array.isArray(v)) v.forEach((x) => p.append(k, x))
    else p.set(k, String(v))
  }
  const s = p.toString()
  return s ? `?${s}` : ''
}

export function EligibilityQueuePage() {
  const { user, hasRole } = useAuth()
  const canEdit = hasRole('posting_team', 'super_admin')
  const [meta, setMeta] = useState<Meta | null>(null)
  const [kpis, setKpis] = useState<Record<string, number>>({})
  const [charts, setCharts] = useState<{
    by_facility: Array<{ key: string; value: number }>
    by_user: Array<{ key: string; value: number }>
    by_status: Array<{ key: string; value: number }>
  }>({ by_facility: [], by_user: [], by_status: [] })
  const [items, setItems] = useState<WorkItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(1)
  const [q, setQ] = useState('')
  const [facility, setFacility] = useState<string[]>([])
  const [month, setMonth] = useState<string[]>([])
  const [status, setStatus] = useState<string[]>([])
  const [insurance, setInsurance] = useState<string[]>([])
  const [assignedTo, setAssignedTo] = useState<string[]>([])
  const [mine, setMine] = useState(hasRole('posting_team') && !hasRole('super_admin'))
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<{
    item: WorkItem
    history: Array<Record<string, unknown>>
    attachments: Array<Record<string, unknown>>
  } | null>(null)
  const [toast, setToast] = useState<{ message: string; tone: 'info' | 'error' | 'success' } | null>(
    null,
  )
  const [editStatus, setEditStatus] = useState('')
  const [editRef, setEditRef] = useState('')
  const [editNotes, setEditNotes] = useState('')
  const [reasonKey, setReasonKey] = useState('verified_by_phone')
  const [reasonText, setReasonText] = useState('')
  const [assignees, setAssignees] = useState<Array<{ user_id: string; display_name: string }>>([])
  const [busy, setBusy] = useState(false)

  const filterParams = useMemo(() => {
    const assigned = mine && user ? [user.user_id] : assignedTo
    return {
      q: q || undefined,
      facility,
      month,
      status,
      insurance,
      assigned_to: assigned,
      page,
      page_size: 50,
      sort_by: 'dos',
      sort_dir: 'desc',
    }
  }, [q, facility, month, status, insurance, assignedTo, mine, user, page])

  const loadList = useCallback(async () => {
    const data = await api<{
      items: WorkItem[]
      total: number
      pages: number
    }>(`/api/eligibility/items${qs(filterParams)}`)
    setItems(data.items)
    setTotal(data.total)
    setPages(data.pages || 1)
  }, [filterParams])

  const loadDash = useCallback(async () => {
    const [k, c, m] = await Promise.all([
      api<Record<string, number>>(`/api/eligibility/kpis${qs({ facility, month })}`),
      api<typeof charts>(`/api/eligibility/charts${qs({ facility, month })}`),
      api<Meta>('/api/eligibility/meta'),
    ])
    setKpis(k)
    setCharts(c)
    setMeta(m)
  }, [facility, month])

  useEffect(() => {
    void loadDash().catch((e) =>
      setToast({ message: String(e.message || e), tone: 'error' }),
    )
  }, [loadDash])

  useEffect(() => {
    void loadList().catch((e) =>
      setToast({ message: String(e.message || e), tone: 'error' }),
    )
  }, [loadList])

  useEffect(() => {
    if (!canEdit) return
    void api<Array<{ user_id: string; display_name: string }>>('/api/eligibility/posting-users')
      .then(setAssignees)
      .catch(() => undefined)
  }, [canEdit])

  async function openItem(id: string) {
    setSelectedId(id)
    try {
      if (canEdit) {
        try {
          await api(`/api/eligibility/items/${id}/lock`, { method: 'POST' })
        } catch (e) {
          if (e instanceof ApiError && e.status === 409) {
            const d = e.detail as { message?: string }
            setToast({
              message: d?.message || 'Locked for editing',
              tone: 'error',
            })
          }
        }
      }
      const data = await api<{
        item: WorkItem
        history: Array<Record<string, unknown>>
        attachments: Array<Record<string, unknown>>
      }>(`/api/eligibility/items/${id}`)
      setDetail(data)
      setEditStatus(data.item.eligibility_status)
      setEditRef(data.item.reference_number || '')
      setEditNotes(data.item.notes || '')
    } catch (e) {
      setToast({ message: String((e as Error).message), tone: 'error' })
    }
  }

  async function closeDrawer() {
    if (selectedId && canEdit) {
      try {
        await api(`/api/eligibility/items/${selectedId}/unlock`, { method: 'POST' })
      } catch {
        /* ignore */
      }
    }
    setSelectedId(null)
    setDetail(null)
  }

  useEffect(() => {
    if (!selectedId || !canEdit) return
    const t = window.setInterval(() => {
      void api(`/api/eligibility/items/${selectedId}/heartbeat`, { method: 'POST' }).catch(
        () => undefined,
      )
    }, 60_000)
    return () => window.clearInterval(t)
  }, [selectedId, canEdit])

  async function saveEdits() {
    if (!selectedId) return
    setBusy(true)
    try {
      await api(`/api/eligibility/items/${selectedId}`, {
        method: 'PATCH',
        body: JSON.stringify({
          eligibility_status: editStatus,
          reference_number: editRef,
          notes: editNotes,
          reason_key: reasonKey,
          reason_text: reasonText || null,
        }),
      })
      setToast({ message: 'Saved', tone: 'success' })
      await openItem(selectedId)
      await loadList()
      await loadDash()
    } catch (e) {
      setToast({ message: String((e as Error).message), tone: 'error' })
    } finally {
      setBusy(false)
    }
  }

  async function selfAssign() {
    if (!selectedId || !user) return
    await api(`/api/eligibility/items/${selectedId}/assign`, {
      method: 'POST',
      body: JSON.stringify({ assigned_to: user.user_id }),
    })
    await openItem(selectedId)
    await loadList()
  }

  async function reassign(uid: string) {
    if (!selectedId) return
    await api(`/api/eligibility/items/${selectedId}/assign`, {
      method: 'POST',
      body: JSON.stringify({ assigned_to: uid || null }),
    })
    await openItem(selectedId)
    await loadList()
  }

  function exportExcel() {
    const url = `/api/eligibility/items/export${qs({
      q: q || undefined,
      facility,
      month,
      status,
      insurance,
      assigned_to: mine && user ? [user.user_id] : assignedTo,
    })}`
    const a = document.createElement('a')
    a.href = url
    // fetch with auth then blob
    void fetch(url, { headers: { Authorization: `Bearer ${getToken()}` } })
      .then((r) => r.blob())
      .then((blob) => {
        const obj = URL.createObjectURL(blob)
        a.href = obj
        a.download = 'eligibility_queue.xlsx'
        a.click()
        URL.revokeObjectURL(obj)
      })
  }

  const reasonNeedsText = meta?.reasons.find((r) => r.reason_key === reasonKey)?.requires_text

  return (
    <div className="space-y-6">
      {toast && (
        <Toast
          message={toast.message}
          tone={toast.tone}
          onDismiss={() => setToast(null)}
        />
      )}

      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-display text-2xl font-semibold text-gray-900 dark:text-white">
            Eligibility Work Queue
          </h1>
          <p className="mt-1 text-sm text-gray-500">
            Operational queue for posting — not an Excel sheet.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" type="button" onClick={exportExcel}>
            Export Excel
          </Button>
          {hasRole('super_admin') && (
            <Button
              type="button"
              onClick={() =>
                void api('/api/eligibility/generate', { method: 'POST' })
                  .then(() => {
                    setToast({ message: 'Work items generated', tone: 'success' })
                    return Promise.all([loadList(), loadDash()])
                  })
                  .catch((e) => setToast({ message: String(e.message), tone: 'error' }))
              }
            >
              Generate from Recon
            </Button>
          )}
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-6">
        <KpiCard label="Pending" value={kpis.pending ?? 0} />
        <KpiCard label="In Progress" value={kpis.in_progress ?? 0} tone="info" />
        <KpiCard label="Waiting Insurance" value={kpis.waiting_insurance ?? 0} tone="warn" />
        <KpiCard label="Waiting Patient" value={kpis.waiting_patient ?? 0} tone="warn" />
        <KpiCard label="Completed Today" value={kpis.completed_today ?? 0} tone="ok" />
        <KpiCard label="Overdue" value={kpis.overdue ?? 0} tone="danger" />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        {(
          [
            ['Cases by Facility', charts.by_facility],
            ['Cases by User', charts.by_user],
            ['Cases by Status', charts.by_status],
          ] as Array<[string, Array<{ key: string; value: number }>]>
        ).map(([title, data]) => (
          <Card key={title} title={title}>
            <div className="h-48">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={data}>
                  <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
                  <XAxis dataKey="key" hide />
                  <YAxis width={36} />
                  <Tooltip />
                  <Bar dataKey="value" fill="#2563eb" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </Card>
        ))}
      </div>

      <Card>
        <div className="mb-4 grid gap-3 md:grid-cols-6">
          <Input
            placeholder="Search patient, EMR, notes…"
            value={q}
            onChange={(e) => {
              setPage(1)
              setQ(e.target.value)
            }}
            className="md:col-span-2"
          />
          <Select
            value={facility[0] || ''}
            onChange={(e) => {
              setPage(1)
              setFacility(e.target.value ? [e.target.value] : [])
            }}
          >
            <option value="">All facilities</option>
            {(meta?.filters.facility || []).map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </Select>
          <Select
            value={month[0] || ''}
            onChange={(e) => {
              setPage(1)
              setMonth(e.target.value ? [e.target.value] : [])
            }}
          >
            <option value="">All months</option>
            {(meta?.filters.month || []).map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </Select>
          <Select
            value={status[0] || ''}
            onChange={(e) => {
              setPage(1)
              setStatus(e.target.value ? [e.target.value] : [])
            }}
          >
            <option value="">All statuses</option>
            {(meta?.statuses || []).map((s) => (
              <option key={s.status_key} value={s.status_key}>
                {s.display_name}
              </option>
            ))}
          </Select>
          <Select
            value={insurance[0] || ''}
            onChange={(e) => {
              setPage(1)
              setInsurance(e.target.value ? [e.target.value] : [])
            }}
          >
            <option value="">All insurance</option>
            {(meta?.filters.insurance || []).map((i) => (
              <option key={i} value={i}>
                {i}
              </option>
            ))}
          </Select>
        </div>
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-300">
            <input
              type="checkbox"
              checked={mine}
              onChange={(e) => {
                setPage(1)
                setMine(e.target.checked)
              }}
            />
            Assigned to me
          </label>
          <span className="text-sm text-gray-500">{total.toLocaleString()} items</span>
        </div>

        <div className="overflow-x-auto rounded-xl border border-gray-100 dark:border-gray-800">
          <table className="min-w-full text-left text-sm">
            <thead className="bg-gray-50 text-xs uppercase tracking-wide text-gray-500 dark:bg-gray-800/80 dark:text-gray-400">
              <tr>
                {[
                  'Patient',
                  'DOS',
                  'DOB',
                  'Facility',
                  'Insurance',
                  'Status',
                  'Reference',
                  'Notes',
                  'Assigned',
                  'Updated By',
                  'Updated At',
                ].map((h) => (
                  <th key={h} className="whitespace-nowrap px-3 py-3 font-semibold">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((row) => (
                <tr
                  key={row.work_item_id}
                  className="cursor-pointer border-t border-gray-100 hover:bg-brand-50/40 dark:border-gray-800 dark:hover:bg-brand-700/10"
                  onClick={() => void openItem(row.work_item_id)}
                >
                  <td className="px-3 py-2.5 font-medium text-gray-900 dark:text-gray-100">
                    {row.patient_name || row.emr_patient_id}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2.5">{row.dos}</td>
                  <td className="whitespace-nowrap px-3 py-2.5">{row.dob || '—'}</td>
                  <td className="px-3 py-2.5">{row.facility_name}</td>
                  <td className="px-3 py-2.5">{row.insurance_name || '—'}</td>
                  <td className="px-3 py-2.5">
                    <Badge tone={statusTone(row.eligibility_status)}>
                      {row.eligibility_status}
                    </Badge>
                  </td>
                  <td className="px-3 py-2.5">{row.reference_number || '—'}</td>
                  <td className="max-w-[180px] truncate px-3 py-2.5">{row.notes || '—'}</td>
                  <td className="px-3 py-2.5">{row.assigned_to_name || '—'}</td>
                  <td className="px-3 py-2.5">{row.updated_by_name || '—'}</td>
                  <td className="whitespace-nowrap px-3 py-2.5">
                    {row.updated_at ? String(row.updated_at).slice(0, 16).replace('T', ' ') : '—'}
                  </td>
                </tr>
              ))}
              {!items.length && (
                <tr>
                  <td colSpan={11} className="px-3 py-10 text-center text-gray-500">
                    No work items yet. Super Admin can generate from reconciliation.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="mt-4 flex items-center justify-between">
          <Button
            variant="secondary"
            type="button"
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            Previous
          </Button>
          <span className="text-sm text-gray-500">
            Page {page} / {pages}
          </span>
          <Button
            variant="secondary"
            type="button"
            disabled={page >= pages}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
          </Button>
        </div>
      </Card>

      <Drawer
        open={!!selectedId && !!detail}
        onClose={() => void closeDrawer()}
        title={detail?.item.patient_name || 'Work item'}
        wide
      >
        {detail && (
          <div className="space-y-5">
            {detail.item.locked_by_name && detail.item.locked_by !== user?.user_id && (
              <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
                Editing by {detail.item.locked_by_name}…
              </div>
            )}

            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <div className="text-xs text-gray-500">EMR / DOS</div>
                <div className="font-medium">
                  {detail.item.emr_patient_id} · {detail.item.dos}
                </div>
              </div>
              <div>
                <div className="text-xs text-gray-500">Facility / Insurance</div>
                <div className="font-medium">
                  {detail.item.facility_name} · {detail.item.insurance_name || '—'}
                </div>
              </div>
            </div>

            {canEdit ? (
              <>
                <label className="block text-sm">
                  <span className="mb-1 block font-medium">Eligibility Status</span>
                  <Select value={editStatus} onChange={(e) => setEditStatus(e.target.value)}>
                    {(meta?.statuses || []).map((s) => (
                      <option key={s.status_key} value={s.status_key}>
                        {s.display_name}
                      </option>
                    ))}
                  </Select>
                </label>
                <label className="block text-sm">
                  <span className="mb-1 block font-medium">Reference Number</span>
                  <Input value={editRef} onChange={(e) => setEditRef(e.target.value)} />
                </label>
                <label className="block text-sm">
                  <span className="mb-1 block font-medium">Notes</span>
                  <TextArea
                    rows={3}
                    value={editNotes}
                    onChange={(e) => setEditNotes(e.target.value)}
                  />
                </label>
                <div className="grid gap-3 sm:grid-cols-2">
                  <label className="block text-sm">
                    <span className="mb-1 block font-medium">Change reason</span>
                    <Select value={reasonKey} onChange={(e) => setReasonKey(e.target.value)}>
                      {(meta?.reasons || []).map((r) => (
                        <option key={r.reason_key} value={r.reason_key}>
                          {r.display_name}
                        </option>
                      ))}
                    </Select>
                  </label>
                  {reasonNeedsText && (
                    <label className="block text-sm">
                      <span className="mb-1 block font-medium">Reason detail</span>
                      <Input
                        value={reasonText}
                        onChange={(e) => setReasonText(e.target.value)}
                      />
                    </label>
                  )}
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button type="button" disabled={busy} onClick={() => void saveEdits()}>
                    Save changes
                  </Button>
                  <Button variant="secondary" type="button" onClick={() => void selfAssign()}>
                    Assign to me
                  </Button>
                  {hasRole('super_admin') && (
                    <Select
                      className="max-w-xs"
                      value={detail.item.assigned_to || ''}
                      onChange={(e) => void reassign(e.target.value)}
                    >
                      <option value="">Unassigned</option>
                      {assignees.map((a) => (
                        <option key={a.user_id} value={a.user_id}>
                          {a.display_name}
                        </option>
                      ))}
                    </Select>
                  )}
                </div>
              </>
            ) : (
              <div className="space-y-2 text-sm">
                <div>
                  Status: <Badge tone={statusTone(detail.item.eligibility_status)}>
                    {detail.item.eligibility_status}
                  </Badge>
                </div>
                <div>Reference: {detail.item.reference_number || '—'}</div>
                <div>Notes: {detail.item.notes || '—'}</div>
              </div>
            )}

            <div>
              <h4 className="font-display mb-2 font-semibold">Attachments</h4>
              <div className="space-y-2">
                {(detail.attachments || []).map((a) => (
                  <Button
                    key={String(a.attachment_id)}
                    variant="secondary"
                    type="button"
                    onClick={() => {
                      void fetch(`/api/eligibility/attachments/${a.attachment_id}/file`, {
                        headers: { Authorization: `Bearer ${getToken()}` },
                      })
                        .then((r) => r.blob())
                        .then((blob) => {
                          const url = URL.createObjectURL(blob)
                          window.open(url, '_blank')
                        })
                    }}
                  >
                    Open Eligibility PDF
                  </Button>
                ))}
                {!detail.attachments?.length && (
                  <p className="text-sm text-gray-500">No eligibility PDF linked yet.</p>
                )}
              </div>
            </div>

            <div>
              <h4 className="font-display mb-3 font-semibold">Activity timeline</h4>
              <ol className="space-y-3 border-l border-gray-200 pl-4 dark:border-gray-700">
                {(detail.history || []).map((h) => (
                  <li key={String(h.history_id)} className="relative">
                    <span className="absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full bg-brand-500" />
                    <div className="text-sm font-semibold text-gray-900 dark:text-gray-100">
                      {String(h.changed_by_name || 'System')}
                    </div>
                    <div className="text-sm text-gray-600 dark:text-gray-300">
                      Changed {String(h.column_name)}
                      {h.old_value != null || h.new_value != null
                        ? `: ${h.old_value ?? '∅'} → ${h.new_value ?? '∅'}`
                        : ''}
                    </div>
                    <div className="text-xs text-gray-400">
                      {String(h.changed_at || '').replace('T', ' ').slice(0, 19)}
                      {h.reason_display ? ` · ${h.reason_display}` : ''}
                    </div>
                  </li>
                ))}
                {!detail.history?.length && (
                  <li className="text-sm text-gray-500">No activity yet.</li>
                )}
              </ol>
            </div>
          </div>
        )}
      </Drawer>
    </div>
  )
}
