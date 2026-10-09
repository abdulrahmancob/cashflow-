import { useCallback, useEffect, useMemo, useState, type CSSProperties } from 'react'
import { ArrowDown, ArrowUp, Download, Plus, Search, Trash2 } from 'lucide-react'
import { api } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { EmrPatientLink } from '../components/EmrPatientLink'
import { EmptyState, Pagination } from '../components/table'
import { Button, Input, MultiSelect, SearchableSelect, Toast } from '../components/ui'

export const SS_TODAY_REFRESH = 'ss-today-refresh'

type WorkloadStage =
  | 'open'
  | 'working'
  | 'need_action'
  | 'patient_responsibility'
  | 'collected'
  | 'medicare_medicaid'

const NO_SECONDARY_PAYER = 'No Secondary Payer'
const COLLECTED = 'Collected'

function foldInsurance(raw: string) {
  return raw.trim().toLowerCase().replace(/[^a-z0-9]/g, '')
}

function isNoSecondaryPayer(raw: string) {
  return foldInsurance(raw) === foldInsurance(NO_SECONDARY_PAYER)
}

function isCollected(raw: string) {
  return foldInsurance(raw) === foldInsurance(COLLECTED)
}

function isMedicaid(raw: string) {
  return foldInsurance(raw) === foldInsurance('Medicaid')
}

function paymentHasMoney(raw: string | null | undefined) {
  const text = String(raw || '').replace(/[$,]/g, '').trim()
  if (!text) return false
  const n = Number(text)
  if (Number.isFinite(n)) return n !== 0
  return true
}

const ADD_INSURANCE = '__add__'

function insuranceOptions(catalog: string[], extras: string[], current?: string | null) {
  const out: string[] = []
  const seen = new Set<string>()
  for (const name of [...catalog, ...extras, current || '']) {
    const n = name.trim()
    if (!n) continue
    const key = n.toLowerCase()
    if (seen.has(key)) continue
    seen.add(key)
    out.push(n)
  }
  return out
}

type WorkloadRow = {
  revflow_patient_id: string
  dos: string
  carc_kind: 'pr1' | 'pr2'
  emr_patient_id?: string | null
  patient_name?: string | null
  primary_payer?: string | null
  facility_name?: string | null
  second_insurance?: string | null
  submission_date?: string | null
  workload_status?: string | null
  workload_note?: string | null
  payment?: string | null
  provider?: string | null
  submitter?: string | null
  claim_number?: string | null
  paid_date?: string | null
  check_number?: string | null
  tfl_days?: number | null
  tfl_due?: string | null
  tfl_days_left?: number | null
}

type VisitHit = {
  visit_id: string
  revflow_patient_id?: string | null
  emr_patient_id?: string | null
  patient_name?: string | null
  dos: string
  facility_name?: string | null
  primary_payer?: string | null
}

type Meta = {
  filters: {
    facility: string[]
    month: string[]
    second_insurance?: string[]
    submission_date?: string[]
  }
  workload: { statuses: string[]; providers: string[]; submitters: string[] }
}

const STATUS_COLORS: Record<string, { backgroundColor: string; color: string }> = {
  submitted: { backgroundColor: '#0a53a8', color: '#ffffff' },
  paid: { backgroundColor: '#11734b', color: '#ffffff' },
  pending: { backgroundColor: '#ffe5a0', color: '#1a1a1a' },
  'Timely Filing': { backgroundColor: '#b10202', color: '#ffffff' },
  Denied: { backgroundColor: '#7f1d1d', color: '#ffffff' },
  corrected: { backgroundColor: '#ff9907', color: '#1a1a1a' },
}

const SORTABLE = new Set(['emr_patient_id', 'patient_name'])

function statusStyle(value: string): CSSProperties | undefined {
  return STATUS_COLORS[value]
}

function tflLabel(daysLeft: number | null | undefined) {
  if (daysLeft == null) return '—'
  if (daysLeft < 0) return `overdue ${Math.abs(daysLeft)}d`
  if (daysLeft === 0) return 'today'
  return `${daysLeft}d left`
}

function tflStyle(daysLeft: number | null | undefined): CSSProperties | undefined {
  if (daysLeft == null) return undefined
  if (daysLeft <= 0) return { color: '#b10202', fontWeight: 600 }
  if (daysLeft <= 7) return { color: '#b45309', fontWeight: 600 }
  return undefined
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

function fmtDate(d: string | null | undefined): string {
  if (!d) return '—'
  const s = String(d).slice(0, 10)
  const [y, m, day] = s.split('-')
  return `${Number(m)}/${Number(day)}/${y}`
}

function monthLabel(ym: string) {
  const [y, m] = ym.split('-')
  const idx = Number(m) - 1
  if (!y || Number.isNaN(idx) || idx < 0 || idx > 11) return ym
  return `${MONTH_NAMES[idx]}-${y}`
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

function kindLabel(kind: string) {
  return kind === 'pr1' ? 'PR-1' : 'PR-2'
}

function sameRow(a: WorkloadRow, b: WorkloadRow) {
  return (
    a.revflow_patient_id === b.revflow_patient_id && a.dos === b.dos && a.carc_kind === b.carc_kind
  )
}

function rowKeyOf(row: { carc_kind: string; revflow_patient_id: string; dos: string }) {
  return `${row.carc_kind}-${row.revflow_patient_id}-${row.dos}`
}

function visitPayload(row: WorkloadRow) {
  return {
    revflow_patient_id: row.revflow_patient_id,
    dos: String(row.dos).slice(0, 10),
    kind: row.carc_kind,
  }
}

export function EligibilityWorkloadTab({ stage = 'open' }: { stage?: WorkloadStage }) {
  const { viewOnly } = useAuth()
  const open = stage === 'open'
  const working = stage === 'working'
  const needAction = stage === 'need_action'
  const patientResponsibility = stage === 'patient_responsibility'
  const collected = stage === 'collected'
  const medicareMedicaid = stage === 'medicare_medicaid'
  const [items, setItems] = useState<WorkloadRow[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(1)
  const [pageSize, setPageSize] = useState(100)
  const [q, setQ] = useState('')
  const [facility, setFacility] = useState<string[]>([])
  const [month, setMonth] = useState<string[]>([])
  const [kind, setKind] = useState('')
  const [status, setStatus] = useState<string[]>([])
  const [secondInsurance, setSecondInsurance] = useState<string[]>([])
  const [submitterFilter, setSubmitterFilter] = useState<string[]>([])
  const [submissionDates, setSubmissionDates] = useState<string[]>([])
  const [tfl, setTfl] = useState('')
  const [sortBy, setSortBy] = useState('tfl_days_left')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc')
  const [meta, setMeta] = useState<Meta | null>(null)
  const [loading, setLoading] = useState(false)
  const [savingKey, setSavingKey] = useState<string | null>(null)
  const [toast, setToast] = useState<{ message: string; tone: 'info' | 'error' | 'success' } | null>(
    null,
  )
  const [addOpen, setAddOpen] = useState(false)
  const [visitQ, setVisitQ] = useState('')
  const [visitHits, setVisitHits] = useState<VisitHit[]>([])
  const [visitLoading, setVisitLoading] = useState(false)
  const [pickedIds, setPickedIds] = useState<string[]>([])
  const [addKind, setAddKind] = useState('pr1')
  const [addIns, setAddIns] = useState('')
  const [adding, setAdding] = useState(false)
  const [extraInsurance, setExtraInsurance] = useState<string[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [bulkProvider, setBulkProvider] = useState('')
  const [bulkStatus, setBulkStatus] = useState('')
  const [bulkPayment, setBulkPayment] = useState('')
  const [bulkBusy, setBulkBusy] = useState(false)

  const months2026 = useMemo(
    () => (meta?.filters.month || []).filter((m) => m.startsWith('2026')),
    [meta],
  )

  const filterParams = useMemo(
    () => ({
      q: q || undefined,
      facility: facility.length ? facility : undefined,
      month: month.length ? month : undefined,
      kind: collected ? 'pr2' : kind || undefined,
      status: needAction ? ['Denied'] : working && status.length ? status : undefined,
      exclude_status: working ? ['Denied'] : undefined,
      second_insurance: collected
        ? [COLLECTED]
        : patientResponsibility
          ? [NO_SECONDARY_PAYER]
          : secondInsurance.length
            ? secondInsurance
            : undefined,
      exclude_second_insurance:
        patientResponsibility || collected || medicareMedicaid ? undefined : [NO_SECONDARY_PAYER],
      submitter: working && submitterFilter.length ? submitterFilter : undefined,
      submission_date: working && submissionDates.length ? submissionDates : undefined,
      tfl: tfl || undefined,
      has_status: patientResponsibility || collected || medicareMedicaid ? undefined : !open,
      require_moved: patientResponsibility || collected || medicareMedicaid ? false : undefined,
      medicare_medicaid: medicareMedicaid ? true : undefined,
      sort_by: sortBy,
      sort_dir: sortDir,
      page,
      page_size: pageSize,
    }),
    [q, facility, month, kind, status, secondInsurance, submitterFilter, submissionDates, tfl, open, working, needAction, patientResponsibility, collected, medicareMedicaid, sortBy, sortDir, page, pageSize],
  )

  const loadList = useCallback(async () => {
    setLoading(true)
    try {
      const data = await api<{ items: WorkloadRow[]; total: number; pages: number }>(
        `/api/eligibility/workload${qs(filterParams)}`,
      )
      setItems(data.items)
      setTotal(data.total)
      setPages(data.pages || 1)
    } finally {
      setLoading(false)
    }
  }, [filterParams])

  useEffect(() => {
    void api<Meta>('/api/eligibility/pr-meta')
      .then(setMeta)
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    void loadList().catch((e) =>
      setToast({ message: String((e as Error).message || e), tone: 'error' }),
    )
  }, [loadList])

  useEffect(() => {
    if (!addOpen) return
    const needle = visitQ.trim()
    if (needle.length < 2) {
      setVisitHits([])
      return
    }
    const handle = window.setTimeout(() => {
      setVisitLoading(true)
      void api<{ items: VisitHit[] }>(
        `/api/eligibility/workload/visits?q=${encodeURIComponent(needle)}`,
      )
        .then((data) => setVisitHits(data.items || []))
        .catch((e) => setToast({ message: String((e as Error).message || e), tone: 'error' }))
        .finally(() => setVisitLoading(false))
    }, 250)
    return () => window.clearTimeout(handle)
  }, [addOpen, visitQ])

  async function addVisit() {
    if (!pickedIds.length || !addIns.trim()) {
      setToast({ message: 'Pick at least one visit and second insurance', tone: 'error' })
      return
    }
    setAdding(true)
    try {
      await api('/api/eligibility/workload/add', {
        method: 'POST',
        body: JSON.stringify({
          visit_ids: pickedIds,
          kind: addKind,
          second_insurance: addIns.trim(),
        }),
      })
      setToast({
        message:
          pickedIds.length === 1
            ? 'Visit added to Workload'
            : `${pickedIds.length} visits added to Workload`,
        tone: 'success',
      })
      setAddOpen(false)
      setVisitQ('')
      setVisitHits([])
      setPickedIds([])
      setAddIns('')
      await loadList()
    } catch (e) {
      setToast({ message: String((e as Error).message || e), tone: 'error' })
    } finally {
      setAdding(false)
    }
  }

  async function removeVisits(rows: WorkloadRow[]) {
    if (!rows.length) return
    const ok = window.confirm(
      rows.length === 1
        ? 'Remove this visit from Workload?'
        : `Remove ${rows.length} visits from Workload?`,
    )
    if (!ok) return
    setBulkBusy(true)
    try {
      await api('/api/eligibility/workload/remove', {
        method: 'POST',
        body: JSON.stringify({ items: rows.map(visitPayload) }),
      })
      setSelected([])
      await loadList()
    } catch (e) {
      setToast({ message: String((e as Error).message || e), tone: 'error' })
    } finally {
      setBulkBusy(false)
    }
  }

  async function applyBulk() {
    const keys = new Set(selected)
    const rows = items.filter((r) => keys.has(rowKeyOf(r)))
    const patch: Record<string, string> = {}
    if (bulkProvider) patch.provider = bulkProvider
    if (bulkStatus) patch.workload_status = bulkStatus
    if (bulkPayment.trim()) patch.payment = bulkPayment.trim()
    if (!rows.length || !Object.keys(patch).length) {
      setToast({ message: 'Select visits and at least one field', tone: 'error' })
      return
    }
    if ((bulkStatus || '').toLowerCase() === 'paid') {
      const missing = rows.some((r) => !paymentHasMoney(patch.payment || r.payment || ''))
      if (missing) {
        setToast({ message: 'Enter Payment before setting Status to paid', tone: 'error' })
        return
      }
    }
    setBulkBusy(true)
    try {
      await api('/api/eligibility/workload/bulk', {
        method: 'POST',
        body: JSON.stringify({ items: rows.map(visitPayload), ...patch }),
      })
      setSelected([])
      setBulkProvider('')
      setBulkStatus('')
      setBulkPayment('')
      await loadList()
      if (bulkStatus) window.dispatchEvent(new Event(SS_TODAY_REFRESH))
    } catch (e) {
      setToast({ message: String((e as Error).message || e), tone: 'error' })
    } finally {
      setBulkBusy(false)
    }
  }

  function toggleSort(key: string) {
    if (!SORTABLE.has(key)) return
    setPage(1)
    if (sortBy === key) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else {
      setSortBy(key)
      setSortDir('asc')
    }
  }

  function sortHeader(key: string, label: string) {
    return (
      <button
        type="button"
        className="inline-flex items-center gap-1 text-left font-semibold"
        onClick={() => toggleSort(key)}
      >
        <span>{label}</span>
        {sortBy === key ? (
          sortDir === 'desc' ? (
            <ArrowDown className="h-3 w-3" />
          ) : (
            <ArrowUp className="h-3 w-3" />
          )
        ) : null}
      </button>
    )
  }

  function exportExcel() {
    const url = `/api/eligibility/workload/export${qs({
      q: q || undefined,
      facility: facility.length ? facility : undefined,
      month: month.length ? month : undefined,
      kind: collected ? 'pr2' : kind || undefined,
      status: needAction ? ['Denied'] : working && status.length ? status : undefined,
      exclude_status: working ? ['Denied'] : undefined,
      second_insurance: collected
        ? [COLLECTED]
        : patientResponsibility
          ? [NO_SECONDARY_PAYER]
          : secondInsurance.length
            ? secondInsurance
            : undefined,
      exclude_second_insurance:
        patientResponsibility || collected || medicareMedicaid ? undefined : [NO_SECONDARY_PAYER],
      submitter: working && submitterFilter.length ? submitterFilter : undefined,
      submission_date: working && submissionDates.length ? submissionDates : undefined,
      tfl: tfl || undefined,
      has_status: patientResponsibility || collected || medicareMedicaid ? undefined : !open,
      require_moved: patientResponsibility || collected || medicareMedicaid ? false : undefined,
      medicare_medicaid: medicareMedicaid ? true : undefined,
      sort_by: sortBy,
      sort_dir: sortDir,
    })}`
    void fetch(url, { credentials: 'include' })
      .then((r) => r.blob())
      .then((blob) => {
        const obj = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = obj
        a.download = open
          ? 'second_submission_workload.xlsx'
          : needAction
            ? 'second_submission_need_action.xlsx'
            : patientResponsibility
              ? 'second_submission_patient_responsibility.xlsx'
              : collected
                ? 'pr2_collected.xlsx'
                : medicareMedicaid
                  ? 'medicare_medicaid.xlsx'
                  : 'second_submission_action_taken.xlsx'
        a.click()
        URL.revokeObjectURL(obj)
      })
      .catch((e) => setToast({ message: String((e as Error).message || e), tone: 'error' }))
  }

  async function patchRow(row: WorkloadRow, patch: Record<string, string>) {
    const key = `${row.carc_kind}-${row.revflow_patient_id}-${row.dos}`
    const nextStatus =
      'workload_status' in patch ? String(patch.workload_status || '').trim() : row.workload_status || ''
    const nextPayment =
      'payment' in patch ? String(patch.payment || '').trim() : row.payment || ''
    if (nextStatus.toLowerCase() === 'paid' && !paymentHasMoney(nextPayment)) {
      setToast({ message: 'Enter Payment before setting Status to paid', tone: 'error' })
      return
    }
    const nextIns =
      'second_insurance' in patch
        ? String(patch.second_insurance || '').trim()
        : row.second_insurance || ''
    const leaves =
      ('workload_status' in patch && open && nextStatus !== '') ||
      ('workload_status' in patch && working && (nextStatus === '' || nextStatus === 'Denied')) ||
      ('workload_status' in patch && needAction && nextStatus !== 'Denied') ||
      ('second_insurance' in patch && !nextIns) ||
      ('second_insurance' in patch && patientResponsibility && !isNoSecondaryPayer(nextIns)) ||
      ('second_insurance' in patch && !patientResponsibility && !collected && isNoSecondaryPayer(nextIns)) ||
      ('second_insurance' in patch && collected && !isCollected(nextIns)) ||
      ('second_insurance' in patch && !collected && !patientResponsibility && row.carc_kind === 'pr2' && isCollected(nextIns)) ||
      ('second_insurance' in patch && medicareMedicaid && !isMedicaid(nextIns))
    setSavingKey(key)
    setItems((cur) =>
      leaves ? cur.filter((r) => !sameRow(r, row)) : cur.map((r) => (sameRow(r, row) ? { ...r, ...patch } : r)),
    )
    try {
      await api('/api/eligibility/pr-flags', {
        method: 'PATCH',
        body: JSON.stringify({
          revflow_patient_id: row.revflow_patient_id,
          dos: row.dos,
          kind: row.carc_kind,
          ...patch,
        }),
      })
      if (leaves || 'second_insurance' in patch) await loadList()
      if ('workload_status' in patch) window.dispatchEvent(new Event(SS_TODAY_REFRESH))
    } catch (e) {
      await loadList()
      setToast({ message: String((e as Error).message || e), tone: 'error' })
    } finally {
      setSavingKey(null)
    }
  }

  const statuses = meta?.workload.statuses || []
  const providers = meta?.workload.providers || []
  const submitters = meta?.workload.submitters || []

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      {toast && (
        <Toast message={toast.message} tone={toast.tone} onDismiss={() => setToast(null)} />
      )}
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-gray-500">
          {open
            ? `${total.toLocaleString()} visits in workload`
            : needAction
              ? `${total.toLocaleString()} visits need action`
            : patientResponsibility
              ? `${total.toLocaleString()} visits with patient responsibility`
              : medicareMedicaid
                ? `${total.toLocaleString()} medicare-medicaid visits`
                : `${total.toLocaleString()} visits with action taken`}
        </p>
        <div className="flex flex-wrap items-center gap-2">
          {open && !viewOnly ? (
            <Button
              variant="secondary"
              type="button"
              onClick={() => {
                setAddOpen((v) => !v)
                setPickedIds([])
              }}
            >
              <Plus className="h-4 w-4" />
              Add visit
            </Button>
          ) : null}
          <Button variant="secondary" type="button" onClick={exportExcel}>
            <Download className="h-4 w-4" />
            Export Excel
          </Button>
        </div>
      </div>
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900">
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-gray-200 px-3 py-2 dark:border-gray-800">
          <MultiSelect
            value={month}
            onChange={(v) => { setPage(1); setMonth(v) }}
            options={months2026.map((m) => ({ value: m, label: monthLabel(m) }))}
            placeholder="All months"
            className="w-44"
          />
          <Input
            icon={<Search className="h-4 w-4" />}
            value={q}
            placeholder="Search patient, EMR, account…"
            onChange={(e) => {
              setPage(1)
              setQ(e.target.value)
            }}
            className="w-56 shrink-0"
          />
          <MultiSelect
            value={facility}
            onChange={(v) => { setPage(1); setFacility(v) }}
            options={(meta?.filters.facility || []).map((f) => ({ value: f, label: f }))}
            placeholder="All facilities"
            className="w-44"
          />
          {collected ? null : (
          <SearchableSelect
            value={kind}
            onChange={(v) => { setPage(1); setKind(v) }}
            options={[
              { value: '', label: 'PR-1 + PR-2' },
              { value: 'pr2', label: 'PR-2' },
              { value: 'pr1', label: 'PR-1' },
            ]}
            placeholder="PR-1 + PR-2"
            className="w-36"
          />
          )}
          {working ? (
            <>
              <MultiSelect
                value={status}
                onChange={(v) => { setPage(1); setStatus(v) }}
                options={statuses.filter((s) => s !== 'Denied').map((s) => ({ value: s, label: s }))}
                placeholder="All statuses"
                className="w-40"
              />
              <MultiSelect
                value={submitterFilter}
                onChange={(v) => { setPage(1); setSubmitterFilter(v) }}
                options={submitters.map((s) => ({ value: s, label: s }))}
                placeholder="All submitters"
                className="w-44"
              />
              <MultiSelect
                value={submissionDates}
                onChange={(v) => { setPage(1); setSubmissionDates(v) }}
                options={(meta?.filters.submission_date || []).map((d) => ({
                  value: d,
                  label: fmtDate(d),
                }))}
                placeholder="All submission dates"
                className="w-48"
              />
            </>
          ) : null}
          {patientResponsibility || collected || medicareMedicaid ? null : (
            <MultiSelect
              value={secondInsurance}
              onChange={(v) => { setPage(1); setSecondInsurance(v) }}
              options={(meta?.filters.second_insurance || [])
                .filter((name) => !isNoSecondaryPayer(name) && !isCollected(name))
                .map((name) => ({ value: name, label: name }))}
              placeholder="All second insurance"
              className="w-48"
            />
          )}
          <SearchableSelect
            value={tfl}
            onChange={(v) => { setPage(1); setTfl(v) }}
            options={[
              { value: '', label: 'All TFL' },
              { value: 'overdue', label: 'Overdue' },
              { value: 'today', label: 'Due today' },
              { value: 'd7', label: 'Due in 7 days' },
              { value: 'd30', label: 'Due in 30 days' },
              { value: 'none', label: 'No TFL rule' },
            ]}
            placeholder="All TFL"
            className="w-40"
          />
        </div>
        {open && addOpen ? (
          <div className="shrink-0 space-y-2 border-b border-gray-200 px-3 py-2 dark:border-gray-800">
            <Input
              icon={<Search className="h-4 w-4" />}
              value={visitQ}
              placeholder="Search existing visits (name, EMR, DOS)…"
            onChange={(e) => {
              setVisitQ(e.target.value)
              setPickedIds([])
            }}
              className="w-full max-w-xl"
            />
            {visitLoading ? <p className="text-xs text-gray-500">Searching…</p> : null}
            {visitHits.length ? (
              <div className="max-h-40 overflow-auto rounded border border-gray-200 dark:border-gray-700">
                {visitHits.map((hit) => {
                  const on = pickedIds.includes(hit.visit_id)
                  return (
                    <label
                      key={hit.visit_id}
                      className="flex w-full cursor-pointer items-start gap-2 border-b border-gray-100 px-3 py-2 text-left text-sm last:border-b-0 hover:bg-gray-50 dark:border-gray-800 dark:hover:bg-gray-800"
                    >
                      <input
                        type="checkbox"
                        className="mt-1 h-4 w-4 accent-blue-600"
                        checked={on}
                        onChange={() =>
                          setPickedIds((cur) =>
                            on ? cur.filter((id) => id !== hit.visit_id) : [...cur, hit.visit_id],
                          )
                        }
                      />
                      <span>
                        <span className="block font-medium text-gray-900 dark:text-white">
                          {hit.patient_name || '—'} · {fmtDate(hit.dos)}
                        </span>
                        <span className="text-xs text-gray-500">
                          EMR {hit.emr_patient_id || '—'} · {hit.facility_name || '—'} ·{' '}
                          {hit.primary_payer || '—'}
                        </span>
                      </span>
                    </label>
                  )
                })}
              </div>
            ) : null}
            {pickedIds.length ? (
              <div className="flex flex-wrap items-end gap-2">
                <p className="text-sm text-gray-700 dark:text-gray-200">
                  {pickedIds.length} visit{pickedIds.length === 1 ? '' : 's'} selected
                </p>
                <SearchableSelect
                  value={addKind}
                  onChange={setAddKind}
                  options={[
                    { value: 'pr1', label: 'PR-1' },
                    { value: 'pr2', label: 'PR-2' },
                  ]}
                  className="w-28"
                />
                <SearchableSelect
                  value={addIns}
                  onChange={setAddIns}
                  options={(meta?.filters.second_insurance || []).map((name) => ({
                    value: name,
                    label: name,
                  }))}
                  placeholder="Second insurance"
                  className="w-48"
                />
                <Button type="button" disabled={adding || !addIns} onClick={() => void addVisit()}>
                  Add
                </Button>
              </div>
            ) : null}
          </div>
        ) : null}
        {selected.length && !viewOnly ? (
          <div className="flex shrink-0 flex-wrap items-end gap-2 border-b border-gray-200 px-3 py-2 dark:border-gray-800">
            <p className="text-sm text-gray-600 dark:text-gray-300">{selected.length} selected</p>
            <SearchableSelect
              value={bulkProvider}
              onChange={setBulkProvider}
              options={[
                { value: '', label: 'Provider' },
                ...providers.map((s) => ({ value: s, label: s })),
              ]}
              placeholder="Provider"
              className="w-44"
            />
            <SearchableSelect
              value={bulkStatus}
              onChange={setBulkStatus}
              options={[
                { value: '', label: 'Status' },
                ...statuses.map((s) => ({ value: s, label: s })),
              ]}
              placeholder="Status"
              className="w-40"
            />
            <Input
              value={bulkPayment}
              placeholder="Payment"
              onChange={(e) => setBulkPayment(e.target.value)}
              className="w-28"
            />
            <Button
              type="button"
              disabled={bulkBusy}
              onClick={() => void applyBulk()}
            >
              Apply
            </Button>
            <Button
              variant="secondary"
              type="button"
              disabled={bulkBusy}
              onClick={() => {
                const keys = new Set(selected)
                void removeVisits(items.filter((r) => keys.has(rowKeyOf(r))))
              }}
            >
              <Trash2 className="h-4 w-4" />
              Delete
            </Button>
          </div>
        ) : null}
        {items.length ? (
          <div className="table-scroll min-h-0 flex-1 overflow-auto">
            <table className="elig-sheet min-w-full text-left">
              <thead>
                <tr>
                  <th>
                    <input
                      type="checkbox"
                      className="h-4 w-4 accent-blue-600"
                      checked={
                        items.length > 0 && items.every((r) => selected.includes(rowKeyOf(r)))
                      }
                      onChange={(e) => {
                        const keys = items.map(rowKeyOf)
                        if (e.target.checked) {
                          setSelected((cur) => Array.from(new Set([...cur, ...keys])))
                        } else {
                          const drop = new Set(keys)
                          setSelected((cur) => cur.filter((k) => !drop.has(k)))
                        }
                      }}
                    />
                  </th>
                  <th>Kind</th>
                  <th>{sortHeader('emr_patient_id', 'EMR')}</th>
                  <th>{sortHeader('patient_name', 'Patient')}</th>
                  <th>Primary Insurance</th>
                  <th>Secondary Insurance</th>
                  <th>Location</th>
                  <th>DOS</th>
                  <th>TFL</th>
                  <th>Submission Date</th>
                  <th>Payment</th>
                  <th>Note</th>
                  <th>Provider</th>
                  {working || medicareMedicaid ? <th>Submitter</th> : null}
                  <th>Status</th>
                  <th>Claim number</th>
                  <th>Paid date</th>
                  <th>Check number</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => {
                  const rowKey = rowKeyOf(row)
                  const busy = viewOnly || savingKey === rowKey || bulkBusy
                  const checked = selected.includes(rowKey)
                  return (
                    <tr key={rowKey}>
                      <td>
                        <input
                          type="checkbox"
                          className="h-4 w-4 accent-blue-600"
                          checked={checked}
                          onChange={() =>
                            setSelected((cur) =>
                              checked ? cur.filter((k) => k !== rowKey) : [...cur, rowKey],
                            )
                          }
                        />
                      </td>
                      <td>{kindLabel(row.carc_kind)}</td>
                      <td className="text-gray-500">
                        <EmrPatientLink id={row.emr_patient_id} facilityName={row.facility_name} />
                      </td>
                      <td className="font-medium text-gray-900 dark:text-white">
                        {row.patient_name || '—'}
                      </td>
                      <td className="max-w-[160px] truncate" title={row.primary_payer || ''}>
                        {row.primary_payer || '—'}
                      </td>
                      <td onClick={(e) => e.stopPropagation()}>
                        <SearchableSelect
                          value={row.second_insurance || ''}
                          disabled={busy}
                          className="w-44"
                          placeholder="—"
                          onChange={(next) => {
                            if (next === ADD_INSURANCE) {
                              const typed = window.prompt('Second insurance name')?.trim() || ''
                              if (!typed || typed === (row.second_insurance || '')) return
                              setExtraInsurance((cur) =>
                                cur.some((x) => x.toLowerCase() === typed.toLowerCase())
                                  ? cur
                                  : [...cur, typed],
                              )
                              void patchRow(row, { second_insurance: typed })
                              return
                            }
                            if (next === (row.second_insurance || '')) return
                            void patchRow(row, { second_insurance: next })
                          }}
                          options={[
                            { value: '', label: '—' },
                            ...insuranceOptions(
                              meta?.filters.second_insurance || [],
                              extraInsurance,
                              row.second_insurance,
                            ).map((name) => ({ value: name, label: name })),
                            { value: ADD_INSURANCE, label: 'Add new…' },
                          ]}
                        />
                      </td>
                      <td>{row.facility_name || '—'}</td>
                      <td>{fmtDate(row.dos)}</td>
                      <td
                        style={tflStyle(row.tfl_days_left)}
                        title={row.tfl_due ? `Due ${fmtDate(row.tfl_due)}` : undefined}
                      >
                        {tflLabel(row.tfl_days_left)}
                      </td>
                      <td>{fmtDate(row.submission_date)}</td>
                      <td>
                        <input
                          className="w-28 rounded border border-gray-200 bg-white px-2 py-1 text-sm dark:border-gray-700 dark:bg-gray-950"
                          defaultValue={row.payment || ''}
                          disabled={busy}
                          onBlur={(e) => {
                            const next = e.target.value.trim()
                            if (next === (row.payment || '')) return
                            if (
                              (row.workload_status || '').toLowerCase() === 'paid' &&
                              !paymentHasMoney(next)
                            ) {
                              e.target.value = row.payment || ''
                              setToast({
                                message: 'Enter Payment before setting Status to paid',
                                tone: 'error',
                              })
                              return
                            }
                            void patchRow(row, { payment: next })
                          }}
                        />
                      </td>
                      <td>
                        <input
                          className="w-44 rounded border border-gray-200 bg-white px-2 py-1 text-sm dark:border-gray-700 dark:bg-gray-950"
                          defaultValue={row.workload_note || ''}
                          disabled={busy}
                          onBlur={(e) => {
                            const next = e.target.value.trim()
                            if (next === (row.workload_note || '')) return
                            void patchRow(row, { workload_note: next })
                          }}
                        />
                      </td>
                      <td>
                        <SearchableSelect
                          value={row.provider || ''}
                          disabled={busy}
                          className="w-48"
                          placeholder="—"
                          onChange={(v) => void patchRow(row, { provider: v })}
                          options={[
                            { value: '', label: '—' },
                            ...providers.map((s) => ({ value: s, label: s })),
                          ]}
                        />
                      </td>
                      {working || medicareMedicaid ? <td>{row.submitter || '—'}</td> : null}
                      <td>
                        <SearchableSelect
                          value={row.workload_status || ''}
                          disabled={busy}
                          className="w-40"
                          placeholder="—"
                          style={statusStyle(row.workload_status || '')}
                          onChange={(v) => {
                            if (v.toLowerCase() === 'paid' && !paymentHasMoney(row.payment)) {
                              setToast({
                                message: 'Enter Payment before setting Status to paid',
                                tone: 'error',
                              })
                              return
                            }
                            void patchRow(row, { workload_status: v })
                          }}
                          options={[
                            { value: '', label: '—' },
                            ...statuses.map((s) => ({ value: s, label: s })),
                          ]}
                        />
                      </td>
                      <td>
                        <input
                          className="w-32 rounded border border-gray-200 bg-white px-2 py-1 text-sm dark:border-gray-700 dark:bg-gray-950"
                          defaultValue={row.claim_number || ''}
                          disabled={busy}
                          onBlur={(e) => {
                            const next = e.target.value.trim()
                            if (next === (row.claim_number || '')) return
                            void patchRow(row, { claim_number: next })
                          }}
                        />
                      </td>
                      <td>
                        <input
                          type="date"
                          className="rounded border border-gray-200 bg-white px-2 py-1 text-sm dark:border-gray-700 dark:bg-gray-950"
                          value={String(row.paid_date || '').slice(0, 10)}
                          disabled={busy}
                          onChange={(e) => void patchRow(row, { paid_date: e.target.value })}
                        />
                      </td>
                      <td>
                        <input
                          className="w-28 rounded border border-gray-200 bg-white px-2 py-1 text-sm dark:border-gray-700 dark:bg-gray-950"
                          defaultValue={row.check_number || ''}
                          disabled={busy}
                          onBlur={(e) => {
                            const next = e.target.value.trim()
                            if (next === (row.check_number || '')) return
                            void patchRow(row, { check_number: next })
                          }}
                        />
                      </td>
                      <td>
                        <Button
                          variant="secondary"
                          type="button"
                          disabled={busy}
                          onClick={() => void removeVisits([row])}
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
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
              title={
                loading
                  ? 'Loading…'
                  : open
                    ? 'No workload rows'
                    : needAction
                      ? 'No need-action rows'
                      : patientResponsibility
                        ? 'No patient-responsibility rows'
                        : collected
                          ? 'No collected rows'
                          : medicareMedicaid
                            ? 'No medicare-medicaid rows'
                            : 'No action taken rows'
              }
              description={
                loading
                  ? 'Fetching visits ready for second submission…'
                  : open
                    ? 'Mark Second Insurance and Second Submission on a PR-1 or PR-2 visit to move it here. Set a status to send it to Action Taken. Set Denied to send it to Need Action.'
                    : needAction
                      ? 'Set status to Denied on a Workload or Action Taken visit to move it here. Change the status to send it back to Action Taken.'
                      : patientResponsibility
                        ? 'Set Second Insurance to No Secondary Payer on a PR-1 or PR-2 visit to move it here. Change the insurance to send it back.'
                        : collected
                          ? 'Set Second Insurance to Collected on a PR-2 visit to move it here. Change the insurance to send it back to Second Submission.'
                          : medicareMedicaid
                            ? 'Medicare primary with Medicaid second insurance is listed here for the Second Submission Lead.'
                            : 'Set a status on a Workload visit to move it here.'
              }
              icon={<Search className="h-6 w-6" />}
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
    </div>
  )
}
