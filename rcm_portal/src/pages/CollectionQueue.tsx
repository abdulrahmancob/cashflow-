import { useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react'
import { ArrowDown, ArrowUp, Download, Search } from 'lucide-react'
import { api, ApiError } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { TODAY_REFRESH } from '../components/CompletedTodayBadge'
import { EmrPatientLink } from '../components/EmrPatientLink'
import { EmptyState, Pagination } from '../components/table'
import { Button, Input, MultiSelect, SearchableSelect, Toast } from '../components/ui'

type Bucket =
  | 'denied'
  | 'overdue'
  | 'action'
  | 'follow_up'
  | 'arbitration'
  | 'at_risk'
  | 'paid_patient_responsibility'

type CollectionRow = {
  work_item_id: string
  patient_name?: string | null
  emr_patient_id?: string | null
  account_number?: string | null
  insurance_name?: string | null
  facility_name?: string | null
  dos?: string | null
  client_payment?: number | null
  insurance_payment?: number | null
  paid_amount?: number | null
  source_visit_status?: string | null
  work_date?: string | null
  assigned_to?: string | null
  assigned_to_name?: string | null
  assigned_to_code?: string | null
  denial_reason?: string | null
  root_cause?: string | null
  actions_taken?: string | null
  collection_status?: string | null
  manual_overrides?: Record<string, unknown> | null
}

type LookupRow = { lookup_id: string; kind: string; label: string }

type CollectorUser = {
  user_id: string
  display_name: string
  collector_code?: string | null
}

type Meta = {
  filters: {
    facility: string[]
    insurance: string[]
    month: string[]
    visit_status?: string[]
  }
}

const MONTH_NAMES = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
]

const STATUS_COLORS: Record<string, { backgroundColor: string; color: string }> = {
  Pending: { backgroundColor: '#ffe5a0', color: '#1a1a1a' },
  Paid: { backgroundColor: '#11734b', color: '#ffffff' },
  Dead: { backgroundColor: '#374151', color: '#ffffff' },
  'Action Taken': { backgroundColor: '#0a53a8', color: '#ffffff' },
  Arbitration: { backgroundColor: '#5b21b6', color: '#ffffff' },
  'Canceled - No Show': { backgroundColor: '#6b7280', color: '#ffffff' },
  'Submitted without Auth': { backgroundColor: '#ff9907', color: '#1a1a1a' },
}

const SORTABLE = new Set([
  'patient_name',
  'emr_patient_id',
  'account_number',
  'insurance_name',
  'facility_name',
  'dos',
  'source_visit_status',
  'work_date',
  'collection_status',
  'insurance_payment',
])

function monthLabel(ym: string) {
  const [y, m] = ym.split('-')
  const idx = Number(m) - 1
  if (!y || Number.isNaN(idx) || idx < 0 || idx > 11) return ym
  return `${MONTH_NAMES[idx]}-${y}`
}

function fmtDate(d: string | null | undefined) {
  if (!d) return '—'
  const s = String(d).slice(0, 10)
  const [y, m, day] = s.split('-')
  return `${Number(m)}/${Number(day)}/${y}`
}

function money2(n: number | null | undefined) {
  if (n == null || Number.isNaN(Number(n))) return '—'
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
  }).format(Number(n))
}

function visitLabel(status?: string | null) {
  if (!status) return '—'
  return status.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

function qs(params: Record<string, string | string[] | number | boolean | undefined>) {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v == null || v === '') continue
    if (Array.isArray(v)) v.forEach((x) => p.append(k, x))
    else p.set(k, String(v))
  }
  const s = p.toString()
  return s ? `?${s}` : ''
}

function assigneeText(row: CollectionRow) {
  if (row.assigned_to_code && row.assigned_to_name) {
    return `${row.assigned_to_code} — ${row.assigned_to_name}`
  }
  return row.assigned_to_name || row.assigned_to_code || '—'
}

function collectorOptionLabel(user: CollectorUser) {
  return user.collector_code ? `${user.collector_code} — ${user.display_name}` : user.display_name
}

function statusStyle(value: string): CSSProperties | undefined {
  return STATUS_COLORS[value]
}

const BUCKETS: { key: Bucket; label: string }[] = [
  { key: 'denied', label: 'Denied' },
  { key: 'overdue', label: 'Overdue' },
  { key: 'action', label: 'Action' },
  { key: 'follow_up', label: 'Follow up' },
  { key: 'arbitration', label: 'Arbitration' },
  { key: 'at_risk', label: 'At risk' },
  { key: 'paid_patient_responsibility', label: 'Paid - Patient Responsibility' },
]

const CELL_INPUT =
  'rounded border border-gray-200 bg-white px-2 py-1 text-xs dark:border-gray-700 dark:bg-gray-950'

const UNASSIGNED = '__unassigned__'
const BLANK = '__blank__'

function withBlank(options: { value: string; label: string }[]) {
  if (options.some((option) => option.value === BLANK)) return options
  return [{ value: BLANK, label: '(Blank)' }, ...options]
}

function tokenPrefix(short: string, long: string) {
  const head = short.trim().toLowerCase()
  const tail = long.trim().toLowerCase()
  if (!head || head === tail || !tail.startsWith(head)) return false
  const next = tail[head.length]
  return next === ' ' || next === '-' || next === '/' || next === '('
}

function preferInsuranceLabel(current: string, next: string) {
  const currentCaps = current === current.toUpperCase()
  const nextCaps = next === next.toUpperCase()
  if (currentCaps && !nextCaps) return next
  if (nextCaps && !currentCaps) return current
  return current.length <= next.length ? current : next
}

function groupInsurance(names: string[]) {
  const byFold = new Map<string, string>()
  for (const raw of names) {
    const text = raw.trim()
    if (!text || text === BLANK) continue
    const key = text.toLowerCase()
    const current = byFold.get(key)
    byFold.set(key, current ? preferInsuranceLabel(current, text) : text)
  }
  const unique = [...byFold.values()].sort((a, b) =>
    a.localeCompare(b, undefined, { sensitivity: 'base' }),
  )
  const parentOf = new Map<string, string>()
  for (const name of unique) {
    let best: string | null = null
    for (const candidate of unique) {
      if (!tokenPrefix(candidate, name)) continue
      if (!best || candidate.length < best.length) best = candidate
    }
    if (best) parentOf.set(name.toLowerCase(), best)
  }
  const grouped = new Map<string, { label: string; value: string; children: { label: string; value: string }[] }>()
  for (const name of unique) {
    const parent = parentOf.get(name.toLowerCase())
    if (!parent) continue
    const key = parent.toLowerCase()
    let group = grouped.get(key)
    if (!group) {
      group = { label: parent, value: `group:${key}`, children: [{ label: parent, value: parent }] }
      grouped.set(key, group)
    }
    group.children.push({ label: name, value: name })
  }
  const options: { label: string; value: string; children?: { label: string; value: string }[] }[] = [
    { value: BLANK, label: '(Blank)' },
  ]
  const nested = new Set<string>()
  for (const group of grouped.values()) {
    nested.add(group.label.toLowerCase())
    for (const child of group.children) nested.add(child.value.toLowerCase())
  }
  for (const name of unique) {
    const key = name.toLowerCase()
    const group = grouped.get(key)
    if (group) {
      options.push(group)
      continue
    }
    if (nested.has(key)) continue
    options.push({ label: name, value: name })
  }
  return options
}

function paymentText(row: CollectionRow) {
  const n = row.insurance_payment ?? row.paid_amount
  if (n == null || Number.isNaN(Number(n))) return ''
  return String(n)
}

function paymentEdited(row: CollectionRow) {
  const ov = row.manual_overrides
  if (!ov) return false
  for (const key of ['insurance_payment', 'paid_amount']) {
    const raw = ov[key]
    if (raw == null || String(raw).trim() === '') continue
    if (!Number.isNaN(Number(raw))) return true
  }
  return false
}

function statusTab(status: string, visitStatus?: string | null): Bucket | null {
  const folded = status.toLowerCase().replace(/[^a-z0-9]+/g, '')
  if (folded === 'paid' && (visitStatus || '').toLowerCase() === 'patient_responsibility') {
    return 'paid_patient_responsibility'
  }
  if (folded === 'arbitration') return 'arbitration'
  if (folded === 'actiontaken' || folded === 'pending') return 'action'
  if (folded === 'submittedwithoutauth') return 'at_risk'
  return null
}

function emptyCopy(bucket: Bucket, loading: boolean) {
  if (loading) {
    return { title: 'Loading…', description: 'Fetching collection visits…' }
  }
  if (bucket === 'overdue') {
    return {
      title: 'No overdue visits',
      description: 'Pending visits appear here after the insurance-behavior SLA is overdue.',
    }
  }
  if (bucket === 'action') {
    return {
      title: 'No action visits',
      description: 'Visits marked Action Taken or Pending appear here.',
    }
  }
  if (bucket === 'follow_up') {
    return {
      title: 'No follow up visits',
      description: 'Action visits appear here 30 days after they were moved.',
    }
  }
  if (bucket === 'arbitration') {
    return {
      title: 'No arbitration visits',
      description: 'Visits marked Arbitration appear here.',
    }
  }
  if (bucket === 'at_risk') {
    return {
      title: 'No at risk visits',
      description: 'Visits marked Submitted without Auth appear here.',
    }
  }
  if (bucket === 'paid_patient_responsibility') {
    return {
      title: 'No paid patient responsibility visits',
      description: 'PR-3 visits appear here after Collection Status is set to Paid.',
    }
  }
  return {
    title: 'No denied visits',
    description: 'Denied visits will appear here for collection work.',
  }
}

function optionsFor(labels: string[], current?: string | null) {
  const out: { value: string; label: string }[] = [{ value: '', label: '—' }]
  const seen = new Set<string>()
  for (const name of [...labels, current || '']) {
    const n = name.trim()
    if (!n) continue
    const key = n.toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    out.push({ value: n, label: n })
  }
  return out
}

export function CollectionQueueTab() {
  const { hasRole } = useAuth()
  const canAssign = hasRole('ops_admin', 'sub_admin')
  const [bucket, setBucket] = useState<Bucket>('denied')
  const [q, setQ] = useState('')
  const [month, setMonth] = useState<string[]>([])
  const [facility, setFacility] = useState<string[]>([])
  const [insurance, setInsurance] = useState<string[]>([])
  const [visitStatus, setVisitStatus] = useState<string[]>([])
  const [rootCause, setRootCause] = useState<string[]>([])
  const [collectionStatus, setCollectionStatus] = useState<string[]>([])
  const [assigneeFilter, setAssigneeFilter] = useState<string[]>([])
  const [sortBy, setSortBy] = useState('dos')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)
  const [items, setItems] = useState<CollectionRow[]>([])
  const [total, setTotal] = useState(0)
  const [pages, setPages] = useState(1)
  const [loading, setLoading] = useState(false)
  const [savingId, setSavingId] = useState<string | null>(null)
  const [meta, setMeta] = useState<Meta | null>(null)
  const [lookups, setLookups] = useState<Record<string, string[]>>({
    denial_reason: [],
    root_cause: [],
    collection_status: [],
  })
  const [toast, setToast] = useState<{ message: string; tone: 'info' | 'error' | 'success' } | null>(
    null,
  )
  const [collectors, setCollectors] = useState<CollectorUser[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [bulkAssignee, setBulkAssignee] = useState('')
  const [assigning, setAssigning] = useState(false)

  const loadMeta = useCallback(async () => {
    const [sheet, catalog] = await Promise.all([
      api<Meta>('/api/eligibility/meta'),
      api<{ items: LookupRow[]; by_kind?: Record<string, LookupRow[]> }>('/api/collection/lookups'),
    ])
    setMeta(sheet)
    const next: Record<string, string[]> = {
      denial_reason: [],
      root_cause: [],
      collection_status: [],
    }
    const grouped = catalog.by_kind || {}
    for (const kind of Object.keys(next)) {
      const rows = grouped[kind] || catalog.items.filter((r) => r.kind === kind)
      next[kind] = rows.map((r) => r.label)
    }
    setLookups(next)
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    const assigneeIds = assigneeFilter.filter((id) => id !== UNASSIGNED)
    try {
      const data = await api<{ items: CollectionRow[]; total: number; pages: number }>(
        `/api/eligibility/items${qs({
          queue: 'collection',
          bucket,
          q,
          facility,
          month,
          insurance,
          visit_status: visitStatus,
          root_cause: rootCause,
          collection_status: collectionStatus,
          assigned_to: assigneeIds.length ? assigneeIds : undefined,
          unassigned: assigneeFilter.includes(UNASSIGNED) || undefined,
          sort_by: sortBy,
          sort_dir: sortDir,
          page,
          page_size: pageSize,
        })}`,
      )
      setItems(data.items || [])
      setTotal(data.total || 0)
      setPages(data.pages || 1)
    } finally {
      setLoading(false)
    }
  }, [assigneeFilter, bucket, collectionStatus, facility, insurance, month, page, pageSize, q, rootCause, sortBy, sortDir, visitStatus])

  useEffect(() => {
    void loadMeta().catch((e) =>
      setToast({ message: String((e as Error).message || e), tone: 'error' }),
    )
  }, [loadMeta])

  useEffect(() => {
    void load().catch((e) => setToast({ message: String((e as Error).message || e), tone: 'error' }))
  }, [load])

  useEffect(() => {
    void api<CollectorUser[]>('/api/eligibility/posting-users?role=collector')
      .then(setCollectors)
      .catch((e) => setToast({ message: String((e as Error).message || e), tone: 'error' }))
  }, [])

  useEffect(() => {
    const visible = new Set(items.map((row) => row.work_item_id))
    setSelected((cur) => {
      const next = cur.filter((id) => visible.has(id))
      return next.length === cur.length ? cur : next
    })
  }, [items])

  const collectorOptions = useMemo(
    () => [
      { value: '', label: 'Unassigned' },
      ...collectors.map((user) => ({ value: user.user_id, label: collectorOptionLabel(user) })),
    ],
    [collectors],
  )

  const assigneeFilterOptions = useMemo(
    () => [
      { value: UNASSIGNED, label: 'Unassigned' },
      ...collectors.map((user) => ({ value: user.user_id, label: collectorOptionLabel(user) })),
    ],
    [collectors],
  )

  function rowAssigneeOptions(row: CollectionRow) {
    if (!row.assigned_to || collectorOptions.some((option) => option.value === row.assigned_to)) {
      return collectorOptions
    }
    return [...collectorOptions, { value: row.assigned_to, label: assigneeText(row) }]
  }

  const pageIds = items.map((row) => row.work_item_id)
  const allSelected = pageIds.length > 0 && pageIds.every((id) => selected.includes(id))

  async function assignRows(ids: string[], assignedTo: string) {
    if (!ids.length) return
    setAssigning(true)
    try {
      await api('/api/eligibility/items/assign-bulk', {
        method: 'POST',
        body: JSON.stringify({ work_item_ids: ids, assigned_to: assignedTo || null }),
      })
      setSelected((cur) => cur.filter((id) => !ids.includes(id)))
      if (ids.length > 1) setBulkAssignee('')
      setToast({
        message: ids.length === 1 ? 'Assignee updated' : `Assigned ${ids.length} visits`,
        tone: 'success',
      })
      await load()
    } catch (e) {
      setToast({
        message: e instanceof ApiError ? e.message : String((e as Error).message || e),
        tone: 'error',
      })
    } finally {
      setAssigning(false)
    }
  }

  async function patchRow(row: CollectionRow, updates: Record<string, string>) {
    setSavingId(row.work_item_id)
    setItems((cur) =>
      cur.map((item) => (item.work_item_id === row.work_item_id ? { ...item, ...updates } : item)),
    )
    try {
      const data = await api<{ item: CollectionRow }>(`/api/eligibility/items/${row.work_item_id}`, {
        method: 'PATCH',
        body: JSON.stringify(updates),
      })
      if (data.item) {
        const nextStatus = (data.item.source_visit_status || updates.source_visit_status || '').toLowerCase()
        const moved = updates.collection_status
          ? statusTab(updates.collection_status, row.source_visit_status)
          : null
        if (nextStatus === 'paid' || nextStatus === 'deduct' || (moved && moved !== bucket)) {
          setItems((cur) => cur.filter((item) => item.work_item_id !== row.work_item_id))
          setTotal((n) => Math.max(0, n - 1))
        } else {
          setItems((cur) =>
            cur.map((item) => (item.work_item_id === row.work_item_id ? { ...item, ...data.item } : item)),
          )
        }
      }
      if ('collection_status' in updates) window.dispatchEvent(new Event(TODAY_REFRESH))
    } catch (e) {
      setToast({
        message: e instanceof ApiError ? e.message : String((e as Error).message || e),
        tone: 'error',
      })
      await load()
    } finally {
      setSavingId(null)
    }
  }

  function toggleSort(key: string) {
    if (!SORTABLE.has(key)) return
    setPage(1)
    if (sortBy === key) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else {
      setSortBy(key)
      setSortDir(key === 'dos' || key === 'insurance_payment' ? 'desc' : 'asc')
    }
  }

  function sortIcon(key: string) {
    if (sortBy !== key) return null
    return sortDir === 'asc' ? (
      <ArrowUp className="inline h-3 w-3" />
    ) : (
      <ArrowDown className="inline h-3 w-3" />
    )
  }

  function exportExcel() {
    const a = document.createElement('a')
    fetch(
      `/api/eligibility/items/export${qs({
        queue: 'collection',
        bucket,
        q,
        facility,
        month,
        insurance,
        visit_status: visitStatus,
        root_cause: rootCause,
        collection_status: collectionStatus,
        assigned_to: assigneeFilter.filter((id) => id !== UNASSIGNED),
        unassigned: assigneeFilter.includes(UNASSIGNED) || undefined,
        sort_by: sortBy,
        sort_dir: sortDir,
      })}`,
      { credentials: 'include' },
    )
      .then((r) => r.blob())
      .then((blob) => {
        const obj = URL.createObjectURL(blob)
        a.href = obj
        a.download = 'collection.xlsx'
        a.click()
        URL.revokeObjectURL(obj)
      })
      .catch((e) => setToast({ message: String((e as Error).message || e), tone: 'error' }))
  }

  const months = useMemo(() => meta?.filters.month || [], [meta])

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2">
      {toast && (
        <Toast message={toast.message} tone={toast.tone} onDismiss={() => setToast(null)} />
      )}
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900">
        <div className="flex shrink-0 flex-col gap-2 border-b border-gray-200 px-3 py-2 dark:border-gray-800">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex w-fit gap-1 rounded-lg bg-gray-100 p-1 dark:bg-gray-800">
              {BUCKETS.map(({ key, label }) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => {
                    setPage(1)
                    setBucket(key)
                  }}
                  className={`rounded-md px-3 py-1 text-sm font-medium transition ${
                    bucket === key
                      ? 'bg-white text-gray-900 shadow-xs dark:bg-gray-900 dark:text-white'
                      : 'text-gray-600 hover:text-gray-900 dark:text-gray-300 dark:hover:text-white'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-2">
              <p className="text-sm text-gray-500">{total.toLocaleString()} visits</p>
              <Button variant="secondary" size="sm" type="button" onClick={exportExcel}>
                <Download className="h-4 w-4" />
                Export Excel
              </Button>
            </div>
          </div>
          <div className="grid w-full grid-cols-[repeat(auto-fill,minmax(11rem,1fr))] gap-2">
          <Input
            icon={<Search className="h-4 w-4" />}
            value={q}
            placeholder="Search patient, EMR, account, insurance…"
            onChange={(e) => {
              setPage(1)
              setQ(e.target.value)
            }}
            className="w-full min-w-0"
          />
          <MultiSelect
            value={month}
            onChange={(v) => {
              setPage(1)
              setMonth(v)
            }}
            options={months.map((m) => ({ value: m, label: monthLabel(m) }))}
            placeholder="All months"
            className="w-full min-w-0"
          />
          <MultiSelect
            value={facility}
            onChange={(v) => {
              setPage(1)
              setFacility(v)
            }}
            options={withBlank((meta?.filters.facility || []).map((f) => ({ value: f, label: f })))}
            placeholder="All facilities"
            className="w-full min-w-0"
          />
          <MultiSelect
            value={insurance}
            onChange={(v) => {
              setPage(1)
              setInsurance(v)
            }}
            options={groupInsurance(meta?.filters.insurance || [])}
            placeholder="All insurance"
            className="w-full min-w-0"
          />
          <MultiSelect
            value={visitStatus}
            onChange={(v) => {
              setPage(1)
              setVisitStatus(v)
            }}
            options={withBlank(
              (meta?.filters.visit_status || []).map((s) => ({
                value: s,
                label: visitLabel(s),
              })),
            )}
            placeholder="All statuses"
            className="w-full min-w-0"
          />
          <MultiSelect
            value={rootCause}
            onChange={(v) => {
              setPage(1)
              setRootCause(v)
            }}
            options={withBlank((lookups.root_cause || []).map((s) => ({ value: s, label: s })))}
            placeholder="All root causes"
            className="w-full min-w-0"
          />
          <MultiSelect
            value={collectionStatus}
            onChange={(v) => {
              setPage(1)
              setCollectionStatus(v)
            }}
            options={withBlank(
              (lookups.collection_status || []).map((s) => ({ value: s, label: s })),
            )}
            placeholder="All collection status"
            className="w-full min-w-0"
          />
          <MultiSelect
            value={assigneeFilter}
            onChange={(v) => {
              setPage(1)
              setAssigneeFilter(v)
            }}
            options={assigneeFilterOptions}
            placeholder="All assignees"
            className="w-full min-w-0"
          />
          </div>
        </div>
        {canAssign ? (
          <div className="flex flex-wrap items-center gap-2 border-b border-gray-200 px-3 py-2 dark:border-gray-800">
            <p className="text-sm text-gray-700 dark:text-gray-200">
              {selected.length
                ? `${selected.length} selected`
                : 'Select visits in the table, then assign'}
            </p>
            <SearchableSelect
              value={bulkAssignee}
              onChange={setBulkAssignee}
              options={collectorOptions}
              placeholder="Assignee"
              className="w-56"
              disabled={assigning}
            />
            <Button
              size="sm"
              type="button"
              disabled={assigning || selected.length === 0}
              onClick={() => void assignRows(selected, bulkAssignee)}
            >
              Assign
            </Button>
          </div>
        ) : null}
        {items.length ? (
          <div className="table-scroll min-h-0 flex-1 overflow-auto">
            <table className="elig-sheet min-w-full text-left">
              <thead>
                <tr>
                  {canAssign ? (
                    <th className="w-8">
                      <input
                        type="checkbox"
                        className="h-4 w-4 accent-blue-600"
                        checked={allSelected}
                        aria-label="Select all visits on this page"
                        onChange={(e) => setSelected(e.target.checked ? pageIds : [])}
                      />
                    </th>
                  ) : null}
                  <th
                    className="elig-sticky elig-sticky-patient_name w-[140px] max-w-[140px] cursor-pointer"
                    onClick={() => toggleSort('patient_name')}
                  >
                    Patient Name {sortIcon('patient_name')}
                  </th>
                  <th
                    className="elig-sticky elig-sticky-emr_patient_id w-[88px] max-w-[88px] cursor-pointer"
                    onClick={() => toggleSort('emr_patient_id')}
                  >
                    EMR ID {sortIcon('emr_patient_id')}
                  </th>
                  <th
                    className="elig-sticky elig-sticky-dos w-[96px] max-w-[96px] cursor-pointer"
                    onClick={() => toggleSort('dos')}
                  >
                    DOS {sortIcon('dos')}
                  </th>
                  <th className="cursor-pointer" onClick={() => toggleSort('account_number')}>
                    Account # {sortIcon('account_number')}
                  </th>
                  <th className="cursor-pointer" onClick={() => toggleSort('insurance_name')}>
                    Insurance Name {sortIcon('insurance_name')}
                  </th>
                  <th className="cursor-pointer" onClick={() => toggleSort('facility_name')}>
                    Facility {sortIcon('facility_name')}
                  </th>
                  <th className="text-right">Client Payment</th>
                  <th
                    className="cursor-pointer text-right"
                    onClick={() => toggleSort('insurance_payment')}
                  >
                    Insurance Payment {sortIcon('insurance_payment')}
                  </th>
                  <th className="cursor-pointer" onClick={() => toggleSort('source_visit_status')}>
                    Status {sortIcon('source_visit_status')}
                  </th>
                  <th className="cursor-pointer" onClick={() => toggleSort('work_date')}>
                    Work Date {sortIcon('work_date')}
                  </th>
                  <th>Assignee</th>
                  <th>Denial Reason</th>
                  <th>RootCause</th>
                  <th>Actions taken</th>
                  <th className="cursor-pointer" onClick={() => toggleSort('collection_status')}>
                    Collection Status {sortIcon('collection_status')}
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => {
                  const busy = savingId === row.work_item_id
                  return (
                    <tr key={row.work_item_id}>
                      {canAssign ? (
                        <td className="w-8" onClick={(e) => e.stopPropagation()}>
                          <input
                            type="checkbox"
                            className="h-4 w-4 accent-blue-600"
                            checked={selected.includes(row.work_item_id)}
                            aria-label={`Select ${row.patient_name || 'visit'}`}
                            onChange={(e) =>
                              setSelected((cur) =>
                                e.target.checked
                                  ? [...cur, row.work_item_id]
                                  : cur.filter((id) => id !== row.work_item_id),
                              )
                            }
                          />
                        </td>
                      ) : null}
                      <td className="elig-sticky elig-sticky-patient_name w-[140px] max-w-[140px] truncate font-medium text-gray-900 dark:text-white">
                        {row.patient_name || '—'}
                      </td>
                      <td className="elig-sticky elig-sticky-emr_patient_id w-[88px] max-w-[88px] text-gray-500">
                        <EmrPatientLink id={row.emr_patient_id} facilityName={row.facility_name} />
                      </td>
                      <td className="elig-sticky elig-sticky-dos w-[96px] max-w-[96px]">{fmtDate(row.dos)}</td>
                      <td className="text-gray-500">{row.account_number || '—'}</td>
                      <td className="max-w-[160px] truncate" title={row.insurance_name || ''}>
                        {row.insurance_name || '—'}
                      </td>
                      <td className="max-w-[140px] truncate" title={row.facility_name || ''}>
                        {row.facility_name || '—'}
                      </td>
                      <td className="text-right tabular-nums">{money2(row.client_payment)}</td>
                      <td className="text-right" onClick={(e) => e.stopPropagation()}>
                        <input
                          className={`${CELL_INPUT} w-24 text-right tabular-nums`}
                          defaultValue={paymentText(row)}
                          key={`${row.work_item_id}-${paymentText(row)}`}
                          inputMode="decimal"
                          disabled={busy}
                          aria-label="Insurance Payment"
                          onBlur={(e) => {
                            const next = e.target.value.trim()
                            if (next === paymentText(row)) return
                            if (next === '' && !paymentEdited(row)) return
                            void patchRow(row, { insurance_payment: next })
                          }}
                        />
                      </td>
                      <td onClick={(e) => e.stopPropagation()}>
                        <SearchableSelect
                          value={row.source_visit_status || ''}
                          disabled={busy}
                          className="w-28"
                          placeholder="—"
                          onChange={(v) => {
                            if (v === (row.source_visit_status || '')) return
                            if (v.toLowerCase() === 'paid' && !paymentEdited(row)) {
                              setToast({
                                message: 'Enter Insurance Payment before marking the visit Paid',
                                tone: 'error',
                              })
                              return
                            }
                            void patchRow(row, { source_visit_status: v })
                          }}
                          options={optionsFor(
                            meta?.filters.visit_status?.length
                              ? meta.filters.visit_status
                              : ['pending', 'paid', 'partial', 'denied', 'deduct', 'collection', 'patient_responsibility'],
                            row.source_visit_status,
                          ).map((o) => ({ ...o, label: o.value ? visitLabel(o.value) : o.label }))}
                        />
                      </td>
                      <td>{fmtDate(row.work_date)}</td>
                      <td onClick={(e) => e.stopPropagation()}>
                        {canAssign ? (
                          <SearchableSelect
                            value={row.assigned_to || ''}
                            disabled={busy || assigning}
                            className="w-56"
                            placeholder="Unassigned"
                            onChange={(v) => {
                              if (v === (row.assigned_to || '')) return
                              void assignRows([row.work_item_id], v)
                            }}
                            options={rowAssigneeOptions(row)}
                          />
                        ) : (
                          assigneeText(row)
                        )}
                      </td>
                      <td onClick={(e) => e.stopPropagation()}>
                        <SearchableSelect
                          value={row.denial_reason || ''}
                          disabled={busy}
                          className="w-40"
                          placeholder="—"
                          onChange={(v) => {
                            if (v === (row.denial_reason || '')) return
                            void patchRow(row, { denial_reason: v })
                          }}
                          options={optionsFor(lookups.denial_reason, row.denial_reason)}
                        />
                      </td>
                      <td onClick={(e) => e.stopPropagation()}>
                        <SearchableSelect
                          value={row.root_cause || ''}
                          disabled={busy}
                          className="w-40"
                          placeholder="—"
                          onChange={(v) => {
                            if (v === (row.root_cause || '')) return
                            void patchRow(row, { root_cause: v })
                          }}
                          options={optionsFor(lookups.root_cause, row.root_cause)}
                        />
                      </td>
                      <td onClick={(e) => e.stopPropagation()}>
                        <input
                          className={`${CELL_INPUT} w-40`}
                          defaultValue={row.actions_taken || ''}
                          key={`${row.work_item_id}-${row.actions_taken || ''}`}
                          disabled={busy}
                          onBlur={(e) => {
                            const next = e.target.value.trim()
                            if (next === (row.actions_taken || '')) return
                            void patchRow(row, { actions_taken: next })
                          }}
                        />
                      </td>
                      <td onClick={(e) => e.stopPropagation()}>
                        <SearchableSelect
                          value={row.collection_status || ''}
                          disabled={busy}
                          className="w-40"
                          placeholder="—"
                          style={statusStyle(row.collection_status || '')}
                          onChange={(v) => {
                            if (v === (row.collection_status || '')) return
                            void patchRow(row, { collection_status: v })
                          }}
                          options={optionsFor(lookups.collection_status, row.collection_status)}
                        />
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="min-h-0 flex-1">
            <EmptyState
              title={emptyCopy(bucket, loading).title}
              description={emptyCopy(bucket, loading).description}
            />
          </div>
        )}
        <Pagination
          page={page}
          pages={pages}
          onPage={setPage}
          pageSize={pageSize}
          onPageSize={(n) => {
            setPage(1)
            setPageSize(n)
          }}
        />
      </div>
    </div>
  )
}
