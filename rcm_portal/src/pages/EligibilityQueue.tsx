import { useCallback, useEffect, useMemo, useState } from 'react'
import { ArrowDown, ArrowUp, Download, RefreshCw, Search } from 'lucide-react'
import { api, ApiError } from '../api/client'
import { CompletedTodayBadge, TODAY_REFRESH } from '../components/CompletedTodayBadge'
import { useAuth } from '../auth/AuthContext'
import { EmrPatientLink } from '../components/EmrPatientLink'
import { EmptyState, Pagination } from '../components/table'
import {
  Alert,
  Button,
  Drawer,
  Field,
  Input,
  SearchableSelect,
  Select,
  TextArea,
  Toast,
} from '../components/ui'

type WorkItem = {
  work_item_id: string
  patient_name?: string
  emr_patient_id: string
  dos: string
  dob?: string
  facility_name: string
  insurance_name?: string
  source_visit_status?: string
  eligibility_status: string
  reference_number?: string
  notes?: string
  assigned_to?: string
  assigned_to_name?: string
  assigned_to_code?: string | null
  updated_by_name?: string
  updated_at?: string
  locked_by?: string
  locked_by_name?: string
  paid_amount?: number | null
  check_number?: string | null
  check_date?: string | null
  tracker_date?: string | null
  pending_reason?: string | null
  client_payment?: number | null
  insurance_payment?: number | null
  updated_payment?: number | null
  coinsurance_payment?: number | null
  rtm?: number | null
  reduction?: number | null
  details?: string | null
  total_amount?: number | null
  added_amount?: number | null
  deducted_amount?: number | null
  insurance_check_number?: string | null
  insurance_check_date?: string | null
  insurance_check_amount?: number | null
  secondary_check_number?: string | null
  secondary_check_date?: string | null
  secondary_check_amount?: number | null
  collector_1?: string | null
  posting_date_1?: string | null
  collector_2?: string | null
  posting_date_2?: string | null
  collector_3?: string | null
  posting_date_3?: string | null
  visit_status_sheet?: string | null
  corrected?: string | null
  corrected_date?: string | null
  sf_visit_id?: string | null
  insurance_id?: string | null
  secondary_insurance?: string | null
  secondary_insurance_id?: string | null
  charged_amount?: number | null
  adjusted?: number | null
  updated_check_number?: string | null
  updated_check_date?: string | null
  updated_check_amount?: number | null
  fourth_check_number?: string | null
  fourth_check_date?: string | null
  fourth_check_amount?: number | null
  work_status?: string | null
  work_date?: string | null
  denial_reason?: string | null
  root_cause?: string | null
  actions_taken?: string | null
  collection_status?: string | null
  collection_tab?: string | null
  context?: Record<string, unknown>
}

type LedgerRow = {
  ledger_id: string
  column_name: string
  amount: number
  check_number?: string | null
  check_date?: string | null
  source?: string
  note?: string | null
  created_by_name?: string | null
  created_at?: string
}

type ColKind = 'text' | 'money' | 'date' | 'status'

type SheetCol = {
  key: keyof WorkItem | 'assigned_to'
  label: string
  kind?: ColKind
  sticky?: boolean
  readonly?: boolean
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
    visit_status: string[]
    check_date: string[]
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

function monthLabel(ym: string) {
  const [y, m] = ym.split('-')
  const idx = Number(m) - 1
  if (!y || Number.isNaN(idx) || idx < 0 || idx > 11) return ym
  return `${MONTH_NAMES[idx]}-${y}`
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
  return status
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

function visitStatusLabel(row: Pick<WorkItem, 'source_visit_status' | 'pending_reason'>) {
  const status = (row.source_visit_status || '').toLowerCase()
  const reason = (row.pending_reason || '').toLowerCase()
  if (status === 'paid' && reason === 'pending_tracker') return 'Paid (Pending Tracker)'
  if (status === 'pending' && reason === 'pending_tracker') return 'Pending (Tracker)'
  return visitLabel(row.source_visit_status)
}

function visitStatusTitle(row: Pick<WorkItem, 'source_visit_status' | 'pending_reason'>) {
  const status = (row.source_visit_status || '').toLowerCase()
  const reason = (row.pending_reason || '').toLowerCase()
  if (status === 'paid' && reason === 'pending_tracker') {
    return 'Paid from remits — one or more checks not in Transaction Tracker yet'
  }
  if (status === 'pending' && reason === 'pending_tracker') {
    return 'Waystar remit received — check not in Transaction Tracker yet'
  }
  if (status === 'pending' && reason === 'no_waystar_remit') {
    return 'No Waystar remit yet'
  }
  return undefined
}

function dateInput(value?: string | null) {
  if (!value) return ''
  return String(value).slice(0, 10)
}

function paidInput(value?: number | null) {
  if (value == null || Number.isNaN(Number(value))) return ''
  return String(value)
}

function colByKey(key: string) {
  return ALL_COLUMNS.find((c) => c.key === key)
}

function itemField(row: WorkItem, key: string): unknown {
  if (key === 'assigned_to') return row.assigned_to_code || row.assigned_to_name || ''
  if (key === 'collector_1') {
    return row.collector_1 || row.assigned_to_code || row.assigned_to_name || ''
  }
  if (key === 'insurance_check_number') {
    return row.insurance_check_number || row.check_number || row.reference_number || ''
  }
  if (key === 'insurance_check_date') return row.insurance_check_date || row.check_date || ''
  if (key === 'insurance_payment') {
    return row.insurance_payment ?? row.paid_amount
  }
  return (row as Record<string, unknown>)[key]
}

function hydrateEdits(item: WorkItem): Record<string, string> {
  const out: Record<string, string> = {}
  for (const col of ALL_COLUMNS) {
    if (col.key === 'assigned_to' || col.readonly) continue
    const raw = itemField(item, col.key)
    if (col.kind === 'money') out[col.key] = paidInput(raw as number | null)
    else if (col.kind === 'date') out[col.key] = dateInput(raw as string | null)
    else out[col.key] = raw == null ? '' : String(raw)
  }
  out.notes = item.notes || ''
  return out
}

function collectionStatusOptions(labels: string[], current?: string | null, emptyLabel = '—') {
  const out: { value: string; label: string }[] = [{ value: '', label: emptyLabel }]
  const seen = new Set<string>()
  for (const name of [...labels, current || '']) {
    const text = name.trim()
    if (!text || seen.has(text.toLowerCase())) continue
    seen.add(text.toLowerCase())
    out.push({ value: text, label: text })
  }
  return out
}

function displayCell(row: WorkItem, col: SheetCol) {
  if (col.kind === 'status') {
    return visitStatusLabel(row)
  }
  if (col.key === 'collection_status') {
    return row.collection_status || row.collection_tab || '—'
  }
  if (col.key === 'assigned_to') {
    return row.assigned_to_code || row.assigned_to_name || '—'
  }
  const raw = itemField(row, col.key)
  if (col.kind === 'money') return money2(raw as number | null)
  if (raw == null || raw === '') return '—'
  return String(raw)
}

const SHEET_COLUMNS: SheetCol[] = [
  { key: 'patient_name', label: 'Patient', sticky: true },
  { key: 'emr_patient_id', label: 'EMR', sticky: true },
  { key: 'dos', label: 'DOS', kind: 'date', sticky: true },
  { key: 'insurance_name', label: 'Insurance' },
  { key: 'insurance_payment', label: 'Insurance Payment', kind: 'money' },
  { key: 'source_visit_status', label: 'Status', kind: 'status' },
  { key: 'collection_status', label: 'Collection Status' },
  { key: 'updated_payment', label: 'Updated Payment', kind: 'money' },
  { key: 'coinsurance_payment', label: 'Co-Insurance', kind: 'money' },
  { key: 'rtm', label: 'RTM', kind: 'money', readonly: true },
  { key: 'reduction', label: 'Reduction', kind: 'money' },
  { key: 'details', label: 'Details' },
  { key: 'total_amount', label: 'Total Amount', kind: 'money', readonly: true },
  { key: 'insurance_check_number', label: 'Ins Check #' },
  { key: 'insurance_check_date', label: 'Ins Check Date', kind: 'date' },
  { key: 'insurance_check_amount', label: 'Ins Check Amt', kind: 'money' },
  { key: 'secondary_check_number', label: '2nd Check #' },
  { key: 'secondary_check_date', label: '2nd Check Date', kind: 'date' },
  { key: 'secondary_check_amount', label: '2nd Check Amt', kind: 'money' },
  { key: 'collector_1', label: 'Collector 1' },
  { key: 'posting_date_1', label: '1st Posting', kind: 'date' },
  { key: 'collector_2', label: 'Collector 2' },
  { key: 'posting_date_2', label: '2nd Posting', kind: 'date' },
  { key: 'collector_3', label: 'Collector 3' },
  { key: 'posting_date_3', label: '3rd Posting', kind: 'date' },
  { key: 'facility_name', label: 'Facility' },
  { key: 'tracker_date', label: 'Tracker Date', kind: 'date' },
  { key: 'added_amount', label: 'Added', kind: 'money', readonly: true },
  { key: 'deducted_amount', label: 'Deducted', kind: 'money', readonly: true },
  { key: 'notes', label: 'Notes' },
  { key: 'assigned_to', label: 'Collector' },
]

const COLLECTION_COLUMNS: SheetCol[] = [
  { key: 'patient_name', label: 'Patient', sticky: true },
  { key: 'emr_patient_id', label: 'EMR', sticky: true },
  { key: 'dos', label: 'DOS', kind: 'date', sticky: true },
  { key: 'insurance_name', label: 'Insurance' },
  { key: 'insurance_payment', label: 'Insurance Payment', kind: 'money' },
  { key: 'source_visit_status', label: 'Status', kind: 'status' },
  { key: 'work_status', label: 'Work Status' },
  { key: 'work_date', label: 'Work Date', kind: 'date' },
  { key: 'denial_reason', label: 'Denial Reason' },
  { key: 'root_cause', label: 'Root Cause' },
  { key: 'actions_taken', label: 'Actions taken' },
  { key: 'collection_status', label: 'Collection Status' },
]

const ALL_COLUMNS: SheetCol[] = [...SHEET_COLUMNS, ...COLLECTION_COLUMNS]

const ADJUST_COLUMNS = [
  { key: 'insurance_payment', label: 'Insurance Payment' },
  { key: 'updated_payment', label: 'Updated Payment' },
  { key: 'coinsurance_payment', label: 'Co-Insurance Payment' },
  { key: 'reduction', label: 'Reduction' },
]

const DRAWER_GROUPS_SHEET: Array<{ title: string; keys: string[] }> = [
  {
    title: 'Identity',
    keys: ['patient_name', 'emr_patient_id', 'dos', 'insurance_name', 'facility_name'],
  },
  {
    title: 'Payments',
    keys: [
      'insurance_payment',
      'updated_payment',
      'coinsurance_payment',
      'rtm',
      'reduction',
      'total_amount',
      'source_visit_status',
      'collection_status',
      'details',
    ],
  },
  {
    title: 'Checks',
    keys: [
      'insurance_check_number',
      'insurance_check_date',
      'insurance_check_amount',
      'secondary_check_number',
      'secondary_check_date',
      'secondary_check_amount',
      'tracker_date',
    ],
  },
  {
    title: 'Collectors',
    keys: [
      'collector_1',
      'posting_date_1',
      'collector_2',
      'posting_date_2',
      'collector_3',
      'posting_date_3',
    ],
  },
]

const DRAWER_GROUPS_COLLECTION: Array<{ title: string; keys: string[] }> = [
  {
    title: 'Identity',
    keys: ['patient_name', 'emr_patient_id', 'dos', 'insurance_name'],
  },
  {
    title: 'Work',
    keys: [
      'insurance_payment',
      'source_visit_status',
      'work_status',
      'work_date',
      'denial_reason',
      'root_cause',
      'actions_taken',
      'collection_status',
    ],
  },
]

export function EligibilityQueuePage({
  queue = 'sheet',
}: {
  queue?: 'sheet' | 'collection'
}) {
  const { user, hasRole } = useAuth()
  const canEdit = true
  const [meta, setMeta] = useState<Meta | null>(null)
  const [items, setItems] = useState<WorkItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(1)
  const [pageSize, setPageSize] = useState(100)
  const [sortBy, setSortBy] = useState('dos')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [q, setQ] = useState('')
  const [facility, setFacility] = useState<string[]>([])
  const [month, setMonth] = useState<string[]>([])
  const [filtersReady, setFiltersReady] = useState(false)
  const [visitStatus, setVisitStatus] = useState<string[]>([])
  const [collectionStatus, setCollectionStatus] = useState<string[]>([])
  const [checkDate, setCheckDate] = useState<string[]>([])
  const [insurance, setInsurance] = useState<string[]>([])
  const [assignedTo] = useState<string[]>([])
  const [mine, setMine] = useState(
    hasRole('posting_team', 'collector') && !hasRole('super_admin', 'ops_admin', 'sub_admin'),
  )
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<{
    item: WorkItem
    history: Array<Record<string, unknown>>
    attachments: Array<Record<string, unknown>>
    ledger: LedgerRow[]
  } | null>(null)
  const [toast, setToast] = useState<{ message: string; tone: 'info' | 'error' | 'success' } | null>(
    null,
  )
  const [collectionStatuses, setCollectionStatuses] = useState<string[]>([])
  const [statusSavingId, setStatusSavingId] = useState<string | null>(null)
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [editCollector, setEditCollector] = useState('')
  const [reasonKey, setReasonKey] = useState('')
  const [reasonText, setReasonText] = useState('')
  const [adjustColumn, setAdjustColumn] = useState('updated_payment')
  const [adjustAmount, setAdjustAmount] = useState('')
  const [adjustCheck, setAdjustCheck] = useState('')
  const [adjustDate, setAdjustDate] = useState('')
  const [adjustNote, setAdjustNote] = useState('')
  const [assignees, setAssignees] = useState<
    Array<{ user_id: string; display_name: string; collector_code?: string | null }>
  >([])
  const [busy, setBusy] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [exporting, setExporting] = useState(false)

  const filterParams = useMemo(() => {
    const assigned = mine && user ? [user.user_id] : assignedTo
    return {
      q: q || undefined,
      facility,
      month,
      visit_status: visitStatus,
      collection_status: collectionStatus,
      check_date: checkDate,
      insurance,
      assigned_to: assigned,
      queue,
      page,
      page_size: pageSize,
      sort_by: sortBy,
      sort_dir: sortDir,
    }
  }, [q, facility, month, visitStatus, collectionStatus, checkDate, insurance, assignedTo, mine, user, queue, page, pageSize, sortBy, sortDir])

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

  const loadMeta = useCallback(async (initMonth = false) => {
    const m = await api<Meta>('/api/eligibility/meta')
    setMeta(m)
    if (initMonth) {
      const months = m.filters.month || []
      const y2026 = months.filter((x) => x.startsWith('2026-'))
      const pick = y2026[0] || months[0]
      if (pick) setMonth([pick])
    }
    return m
  }, [])

  useEffect(() => {
    void api<{ items: Array<{ kind: string; label: string }>; by_kind?: Record<string, Array<{ label: string }>> }>(
      '/api/collection/lookups',
    )
      .then((catalog) => {
        const rows = catalog.by_kind?.collection_status || catalog.items.filter((row) => row.kind === 'collection_status')
        setCollectionStatuses(rows.map((row) => row.label))
      })
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    void loadMeta(true)
      .then(() => setFiltersReady(true))
      .catch((e) => {
        setToast({ message: String(e.message || e), tone: 'error' })
        setFiltersReady(true)
      })
  }, [loadMeta])

  useEffect(() => {
    if (!filtersReady) return
    void loadList().catch((e) =>
      setToast({ message: String(e.message || e), tone: 'error' }),
    )
  }, [filtersReady, loadList])

  useEffect(() => {
    if (!canEdit) return
    void api<
      Array<{ user_id: string; display_name: string; collector_code?: string | null }>
    >('/api/eligibility/posting-users')
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
        ledger: LedgerRow[]
      }>(`/api/eligibility/items/${id}`)
      setDetail({ ...data, ledger: data.ledger || [] })
      const item = data.item
      setEdits(hydrateEdits(item))
      setEditCollector(item.assigned_to || '')
      setAdjustAmount('')
      setAdjustCheck('')
      setAdjustDate('')
      setAdjustNote('')
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

  async function patchCollectionStatus(row: WorkItem, value: string) {
    if (value === (row.collection_status || '')) return
    setStatusSavingId(row.work_item_id)
    try {
      await api(`/api/eligibility/items/${row.work_item_id}`, {
        method: 'PATCH',
        body: JSON.stringify({ collection_status: value || null }),
      })
      window.dispatchEvent(new Event(TODAY_REFRESH))
      await loadList()
    } catch (e) {
      setToast({ message: String((e as Error).message), tone: 'error' })
    } finally {
      setStatusSavingId(null)
    }
  }

  async function saveEdits() {
    if (!selectedId || !detail) return
    const orig = detail.item
    const origEdits = hydrateEdits(orig)
    const payload: Record<string, unknown> = {}
    for (const [key, value] of Object.entries(edits)) {
      const col = colByKey(key)
      if (!col || col.readonly) continue
      if ((origEdits[key] || '') === value) continue
      if (col.kind === 'money') {
        if (value !== '' && Number.isNaN(Number(value))) {
          setToast({ message: `${col.label} must be a number`, tone: 'error' })
          return
        }
        payload[key] = value === '' ? null : Number(value)
      } else {
        payload[key] = value
      }
    }
    if (reasonKey) payload.reason_key = reasonKey
    if (reasonText) payload.reason_text = reasonText
    const fieldChanged = Object.keys(payload).some(
      (k) => k !== 'reason_key' && k !== 'reason_text',
    )
    const collectorChanged = editCollector !== (orig.assigned_to || '')
    if (!fieldChanged && !collectorChanged) {
      setToast({ message: 'No changes', tone: 'info' })
      return
    }
    setBusy(true)
    try {
      if (fieldChanged) {
        await api(`/api/eligibility/items/${selectedId}`, {
          method: 'PATCH',
          body: JSON.stringify(payload),
        })
      }
      if (collectorChanged) {
        await api(`/api/eligibility/items/${selectedId}/assign`, {
          method: 'POST',
          body: JSON.stringify({ assigned_to: editCollector || null }),
        })
      }
      setToast({ message: 'Saved', tone: 'success' })
      window.dispatchEvent(new Event(TODAY_REFRESH))
      await openItem(selectedId)
      await loadList()
    } catch (e) {
      setToast({ message: String((e as Error).message), tone: 'error' })
    } finally {
      setBusy(false)
    }
  }

  async function saveAdjustment() {
    if (!selectedId) return
    if (adjustAmount === '' || Number.isNaN(Number(adjustAmount))) {
      setToast({ message: 'Adjustment amount must be a number', tone: 'error' })
      return
    }
    setBusy(true)
    try {
      await api(`/api/eligibility/items/${selectedId}/adjustments`, {
        method: 'POST',
        body: JSON.stringify({
          column_name: adjustColumn,
          amount: Number(adjustAmount),
          check_number: adjustCheck || null,
          check_date: adjustDate || null,
          note: adjustNote || null,
        }),
      })
      setToast({ message: 'Adjustment saved', tone: 'success' })
      await openItem(selectedId)
      await loadList()
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

  function generateToast(result: Record<string, unknown> | null | undefined) {
    const visitCount = Number(result?.visit_count || 0)
    const errors = Array.isArray(result?.errors) ? result.errors.map(String) : []
    if (!result || visitCount === 0) {
      return { message: errors[0] || 'No recon data found', tone: 'error' as const }
    }
    if (result.ok === false || errors.length) {
      return {
        message: errors[0] || 'Generation finished with errors',
        tone: 'error' as const,
      }
    }
    const enqueued = Number(result.enqueued || 0)
    const closed = Number(result.closed || 0)
    return {
      message: `Updated ${visitCount.toLocaleString()} visits · ${enqueued.toLocaleString()} enqueued · ${closed.toLocaleString()} closed`,
      tone: 'success' as const,
    }
  }

  async function pollGenerateStatus() {
    const deadline = Date.now() + 15 * 60_000
    while (Date.now() < deadline) {
      const status = await api<{
        running: boolean
        last_result?: Record<string, unknown> | null
      }>('/api/eligibility/generate/status')
      if (!status.running) return status.last_result || null
      await new Promise((r) => window.setTimeout(r, 4000))
    }
    throw new Error('Generation is still running — refresh in a few minutes')
  }

  async function generateFromRecon() {
    if (generating) return
    setGenerating(true)
    try {
      try {
        await api('/api/eligibility/generate', { method: 'POST' })
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 409)) throw e
      }
      const result = await pollGenerateStatus()
      setToast(generateToast(result))
      await loadMeta(false)
      await loadList()
    } catch (e) {
      setToast({ message: String((e as Error).message || e), tone: 'error' })
    } finally {
      setGenerating(false)
    }
  }

  async function exportExcel() {
    if (exporting) return
    const url = `/api/eligibility/items/export${qs({
      q: q || undefined,
      facility,
      month,
      visit_status: visitStatus,
      collection_status: collectionStatus,
      check_date: checkDate,
      insurance,
      assigned_to: mine && user ? [user.user_id] : assignedTo,
      queue,
      sort_by: sortBy,
      sort_dir: sortDir,
    })}`
    setExporting(true)
    try {
      const response = await fetch(url, { credentials: 'include' })
      if (!response.ok) {
        let detail = `Export failed (${response.status})`
        try {
          const body = (await response.json()) as { detail?: unknown }
          if (body?.detail) detail = String(body.detail)
        } catch {
          /* error body is not JSON */
        }
        setToast({ message: detail, tone: 'error' })
        return
      }
      const blob = await response.blob()
      const obj = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = obj
      a.download = queue === 'collection' ? 'collection.xlsx' : 'eligibility_sheet.xlsx'
      a.click()
      URL.revokeObjectURL(obj)
    } catch (e) {
      setToast({ message: String((e as Error).message || e), tone: 'error' })
    } finally {
      setExporting(false)
    }
  }

  function toggleSort(key: string) {
    setPage(1)
    if (sortBy === key) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else {
      setSortBy(key)
      setSortDir(key === 'dos' ? 'desc' : 'asc')
    }
  }

  const reasonNeedsText = meta?.reasons.find((r) => r.reason_key === reasonKey)?.requires_text
  const visitStatusTab = visitStatus[0] || 'all'
  const visibleColumns = queue === 'collection' ? COLLECTION_COLUMNS : SHEET_COLUMNS
  const drawerGroups = queue === 'collection' ? DRAWER_GROUPS_COLLECTION : DRAWER_GROUPS_SHEET
  const allVisitStatuses = useMemo(() => {
    const fromMeta = meta?.filters.visit_status || []
    return fromMeta.length
      ? fromMeta
      : ['pending', 'paid', 'partial', 'denied', 'deduct', 'collection', 'patient_responsibility']
  }, [meta])
    const visitStatusOptions = useMemo(() => {
    if (edits.source_visit_status && !allVisitStatuses.includes(edits.source_visit_status)) {
      return [...allVisitStatuses, edits.source_visit_status]
    }
    return allVisitStatuses
  }, [allVisitStatuses, edits.source_visit_status])
  const drawerStatusOptions = useMemo(() => {
    if (edits.source_visit_status && !allVisitStatuses.includes(edits.source_visit_status)) {
      return [...allVisitStatuses, edits.source_visit_status]
    }
    return allVisitStatuses
  }, [allVisitStatuses, edits.source_visit_status])

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      {toast && (
        <Toast message={toast.message} tone={toast.tone} onDismiss={() => setToast(null)} />
      )}

      <div className="flex shrink-0 flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="font-display text-xl font-semibold tracking-tight text-gray-900 dark:text-white">
            {queue === 'collection' ? 'Collection' : 'Eligibility Sheet'}
          </h1>
          <p className="text-sm text-gray-500">
            {total.toLocaleString()} visits · click a row to edit
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {queue !== 'collection' ? <CompletedTodayBadge area="eligibility" /> : null}
          <Button
            variant="secondary"
            type="button"
            disabled={exporting}
            onClick={() => void exportExcel()}
          >
            <Download className="h-4 w-4" />
            {exporting ? 'Exporting…' : 'Export Excel'}
          </Button>
          {hasRole('super_admin', 'sub_admin') && (
            <Button
              type="button"
              disabled={generating}
              onClick={() => void generateFromRecon()}
            >
              <RefreshCw className={`h-4 w-4 ${generating ? 'animate-spin' : ''}`} />
              {generating ? 'Generating…' : 'Generate from Recon'}
            </Button>
          )}
        </div>
      </div>

      <div
        className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900"
      >
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-gray-200 px-3 py-2 dark:border-gray-800">
          <Select
            value={month[0] || ''}
            onChange={(e) => {
              setPage(1)
              setMonth(e.target.value ? [e.target.value] : [])
            }}
            className="w-44"
          >
            <option value="">All months</option>
            {(meta?.filters.month || []).map((m) => (
              <option key={m} value={m}>
                {monthLabel(m)}
              </option>
            ))}
          </Select>
          <Input
            icon={<Search className="h-4 w-4" />}
            value={q}
            placeholder="Search patient, EMR, EFT, notes…"
            onChange={(e) => {
              setPage(1)
              setQ(e.target.value)
            }}
            className="w-56 shrink-0"
          />
          <Select
            value={facility[0] || ''}
            onChange={(e) => {
              setPage(1)
              setFacility(e.target.value ? [e.target.value] : [])
            }}
            className="w-44"
          >
            <option value="">All facilities</option>
            {(meta?.filters.facility || []).map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </Select>
          <Select
            value={insurance[0] || ''}
            onChange={(e) => {
              setPage(1)
              setInsurance(e.target.value ? [e.target.value] : [])
            }}
            className="w-44"
          >
            <option value="">All insurance</option>
            {(meta?.filters.insurance || []).map((i) => (
              <option key={i} value={i}>
                {i}
              </option>
            ))}
          </Select>
          {queue === 'sheet' && (
          <Select
            value={visitStatusTab}
            onChange={(e) => {
              setPage(1)
              setVisitStatus(e.target.value === 'all' || !e.target.value ? [] : [e.target.value])
            }}
            className="w-44"
          >
            <option value="all">All statuses</option>
            {visitStatusOptions.map((s) => (
              <option key={s} value={s}>
                {visitLabel(s)}
              </option>
            ))}
          </Select>
          )}
          <Select
            value={collectionStatus[0] || ''}
            onChange={(e) => {
              setPage(1)
              setCollectionStatus(e.target.value ? [e.target.value] : [])
            }}
            className="w-44"
          >
            <option value="">All collection status</option>
            <option value="__blank__">(Blank)</option>
            {collectionStatuses.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
          <Select
            value={checkDate[0] || ''}
            onChange={(e) => {
              setPage(1)
              setCheckDate(e.target.value ? [e.target.value] : [])
            }}
            className="w-44"
          >
            <option value="">All check dates</option>
            {(meta?.filters.check_date || []).map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </Select>
          <label className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-300">
            <input
              type="checkbox"
              className="rounded border-gray-300 text-brand-600"
              checked={mine}
              onChange={(e) => {
                setPage(1)
                setMine(e.target.checked)
              }}
            />
            Assigned to me
          </label>
        </div>

        {items.length ? (
          <div className="table-scroll min-h-0 flex-1 overflow-auto">
            <table className="elig-sheet min-w-full text-left">
              <thead>
                <tr>
                  {visibleColumns.map((col) => (
                    <th
                      key={col.key}
                      className={col.sticky ? `elig-sticky elig-sticky-${col.key}` : undefined}
                    >
                      <button
                        type="button"
                        className="inline-flex items-center gap-1 text-left font-semibold"
                        onClick={() => toggleSort(col.key)}
                      >
                        <span>{col.label}</span>
                        {sortBy === col.key ? (
                          sortDir === 'desc' ? (
                            <ArrowDown className="h-3 w-3" />
                          ) : (
                            <ArrowUp className="h-3 w-3" />
                          )
                        ) : null}
                      </button>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                    <tr
                      key={row.work_item_id}
                      onClick={() => void openItem(row.work_item_id)}
                    >
                      {visibleColumns.map((col) => {
                        const value = displayCell(row, col)
                        const sticky = col.sticky ? `elig-sticky elig-sticky-${col.key}` : ''
                        const statusClass =
                          col.kind === 'status'
                            ? `elig-status elig-status-${row.source_visit_status || 'pending'}`
                            : ''
                        const moneyClass = col.kind === 'money' ? 'tabular-nums' : ''
                        return (
                          <td
                            key={col.key}
                            className={`${sticky} ${statusClass} ${moneyClass}`.trim()}
                            title={
                              col.kind === 'status'
                                ? visitStatusTitle(row)
                                : value === '—'
                                  ? undefined
                                  : value
                            }
                          >
                            {col.key === 'emr_patient_id' ? (
                              <EmrPatientLink id={row.emr_patient_id} facilityName={row.facility_name} />
                            ) : col.key === 'collection_status' ? (
                              <span onClick={(e) => e.stopPropagation()}>
                                <SearchableSelect
                                  value={row.collection_status || ''}
                                  disabled={statusSavingId === row.work_item_id}
                                  className="w-40"
                                  placeholder={row.collection_tab || '—'}
                                  onChange={(v) => void patchCollectionStatus(row, v)}
                                  options={collectionStatusOptions(
                                    collectionStatuses,
                                    row.collection_status,
                                    row.collection_status ? '—' : row.collection_tab || '—',
                                  )}
                                />
                              </span>
                            ) : (
                              value
                            )}
                          </td>
                        )
                      })}
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="min-h-0 flex-1">
            <EmptyState
              title="No rows found"
              description={
                q
                  ? `Your search “${q}” did not match any visits.`
                  : 'No visits yet. Super Admin can generate from reconciliation.'
              }
              icon={<Search className="h-6 w-6" />}
              action={
                q ? (
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={() => {
                      setQ('')
                      setPage(1)
                    }}
                  >
                    Clear search
                  </Button>
                ) : undefined
              }
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
          pageSizeOptions={[50, 100, 200, 500]}
        />
      </div>

      <Drawer
        open={!!selectedId && !!detail}
        onClose={() => void closeDrawer()}
        title={detail?.item.patient_name || 'Visit'}
        wide
      >
        {detail && (
          <div className="space-y-5">
            {detail.item.locked_by_name && detail.item.locked_by !== user?.user_id && (
              <Alert tone="warning">Editing by {detail.item.locked_by_name}…</Alert>
            )}

            <div>
              <div className="font-display font-semibold text-gray-900 dark:text-white">
                {detail.item.patient_name || detail.item.emr_patient_id}
              </div>
              <div className="text-sm text-gray-500">
                {detail.item.emr_patient_id} · {detail.item.dos}
              </div>
            </div>

            <div className="flex flex-wrap gap-3 text-sm">
              <div className="rounded-md bg-success-50 px-3 py-2 text-success-700 dark:bg-gray-800">
                Added {money2(detail.item.added_amount)}
              </div>
              <div className="rounded-md bg-error-50 px-3 py-2 text-error-700 dark:bg-gray-800">
                Deducted {money2(detail.item.deducted_amount)}
              </div>
              <div className="rounded-md bg-gray-50 px-3 py-2 text-gray-700 dark:bg-gray-800 dark:text-gray-200">
                Total {money2(detail.item.total_amount)}
              </div>
            </div>

            {drawerGroups.map((group) => (
              <div key={group.title}>
                <h4 className="font-display mb-2 font-semibold">{group.title}</h4>
                <div className="grid gap-3 sm:grid-cols-2">
                  {group.keys.map((key) => {
                    const col = colByKey(key)
                    if (!col) return null
                    const fieldLabel = col.label
                    if (col.readonly) {
                      const raw = itemField(detail.item, key)
                      return (
                        <Field key={key} label={fieldLabel}>
                          <Input
                            readOnly
                            value={col.kind === 'money' ? money2(raw as number | null) : String(raw ?? '—')}
                          />
                        </Field>
                      )
                    }
                    if (key === 'source_visit_status') {
                      return (
                        <Field key={key} label={col.label}>
                          <Select
                            value={edits.source_visit_status || ''}
                            onChange={(e) =>
                              setEdits((prev) => ({ ...prev, source_visit_status: e.target.value }))
                            }
                          >
                            <option value="">—</option>
                            {drawerStatusOptions.map((s) => (
                              <option key={s} value={s}>
                                {visitLabel(s)}
                              </option>
                            ))}
                          </Select>
                        </Field>
                      )
                    }
                    if (key === 'collection_status') {
                      return (
                        <Field key={key} label={col.label}>
                          <Select
                            value={edits.collection_status || ''}
                            onChange={(e) =>
                              setEdits((prev) => ({ ...prev, collection_status: e.target.value }))
                            }
                          >
                            <option value="">—</option>
                            {collectionStatusOptions(collectionStatuses, edits.collection_status)
                              .filter((opt) => opt.value)
                              .map((opt) => (
                                <option key={opt.value} value={opt.value}>
                                  {opt.label}
                                </option>
                              ))}
                          </Select>
                          {!edits.collection_status && detail.item.collection_tab ? (
                            <p className="mt-1 text-xs text-gray-500">
                              Collection tab: {detail.item.collection_tab}
                            </p>
                          ) : null}
                        </Field>
                      )
                    }
                    const isNotes = key === 'details'
                    if (isNotes) {
                      return (
                        <Field key={key} label={col.label}>
                          <TextArea
                            rows={2}
                            value={edits[key] || ''}
                            onChange={(e) =>
                              setEdits((prev) => ({ ...prev, [key]: e.target.value }))
                            }
                          />
                        </Field>
                      )
                    }
                    return (
                      <Field key={key} label={fieldLabel}>
                        <Input
                          type={
                            col.kind === 'date' ? 'date' : col.kind === 'money' ? 'number' : 'text'
                          }
                          step={col.kind === 'money' ? '0.01' : undefined}
                          value={edits[key] || ''}
                          onChange={(e) =>
                            setEdits((prev) => ({ ...prev, [key]: e.target.value }))
                          }
                        />
                      </Field>
                    )
                  })}
                </div>
              </div>
            ))}

            <Field label="Collector (queue assignment)">
              <Select
                value={editCollector}
                onChange={(e) => setEditCollector(e.target.value)}
              >
                <option value="">Unassigned</option>
                {assignees.map((a) => (
                  <option key={a.user_id} value={a.user_id}>
                    {a.collector_code
                      ? `${a.collector_code} — ${a.display_name}`
                      : a.display_name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Notes">
              <TextArea
                rows={3}
                value={edits.notes || ''}
                onChange={(e) => setEdits((prev) => ({ ...prev, notes: e.target.value }))}
              />
            </Field>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Change reason (optional)">
                <Select value={reasonKey} onChange={(e) => setReasonKey(e.target.value)}>
                  <option value="">None</option>
                  {(meta?.reasons || []).map((r) => (
                    <option key={r.reason_key} value={r.reason_key}>
                      {r.display_name}
                    </option>
                  ))}
                </Select>
              </Field>
              {reasonNeedsText && (
                <Field label="Reason detail">
                  <Input value={reasonText} onChange={(e) => setReasonText(e.target.value)} />
                </Field>
              )}
            </div>
            <div className="flex flex-wrap gap-2">
              <Button type="button" disabled={busy} onClick={() => void saveEdits()}>
                Save changes
              </Button>
              <Button variant="secondary" type="button" onClick={() => void selfAssign()}>
                Assign to me
              </Button>
            </div>

            <div>
              <h4 className="font-display mb-2 font-semibold">Add adjustment</h4>
              <p className="mb-3 text-sm text-gray-500">
                Positive amount is added; negative is deducted. This updates the column and the ledger.
              </p>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Column">
                  <Select value={adjustColumn} onChange={(e) => setAdjustColumn(e.target.value)}>
                    {ADJUST_COLUMNS.map((c) => (
                      <option key={c.key} value={c.key}>
                        {c.label}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Amount (+ add / − deduct)">
                  <Input
                    type="number"
                    step="0.01"
                    value={adjustAmount}
                    onChange={(e) => setAdjustAmount(e.target.value)}
                  />
                </Field>
                <Field label="Check #">
                  <Input value={adjustCheck} onChange={(e) => setAdjustCheck(e.target.value)} />
                </Field>
                <Field label="Check date">
                  <Input
                    type="date"
                    value={adjustDate}
                    onChange={(e) => setAdjustDate(e.target.value)}
                  />
                </Field>
              </div>
              <Field label="Note">
                <Input value={adjustNote} onChange={(e) => setAdjustNote(e.target.value)} />
              </Field>
              <Button type="button" disabled={busy} onClick={() => void saveAdjustment()}>
                Save adjustment
              </Button>
              <ol className="mt-3 space-y-2">
                {(detail.ledger || []).map((l) => (
                  <li
                    key={l.ledger_id}
                    className="text-sm text-gray-700 dark:text-gray-200"
                  >
                    <span className={Number(l.amount) < 0 ? 'text-error-700' : 'text-success-700'}>
                      {Number(l.amount) < 0 ? '' : '+'}
                      {money2(l.amount)}
                    </span>
                    {' · '}
                    {l.column_name}
                    {l.check_number ? ` · ${l.check_number}` : ''}
                    {l.note ? ` · ${l.note}` : ''}
                    <div className="text-xs text-gray-400">
                      {String(l.created_at || '').replace('T', ' ').slice(0, 19)}
                      {l.created_by_name ? ` · ${l.created_by_name}` : ''}
                      {l.source ? ` · ${l.source}` : ''}
                    </div>
                  </li>
                ))}
                {!detail.ledger?.length && (
                  <li className="text-sm text-gray-500">No money movements yet.</li>
                )}
              </ol>
            </div>

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
                        credentials: 'include',
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
