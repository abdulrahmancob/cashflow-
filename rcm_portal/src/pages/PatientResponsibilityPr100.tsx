import { useCallback, useEffect, useMemo, useState } from 'react'
import { ArrowDown, ArrowUp, Download, Search } from 'lucide-react'
import { api } from '../api/client'
import { EmrPatientLink } from '../components/EmrPatientLink'
import { WaystarAccountLink } from '../components/WaystarAccountLink'
import { EmptyState, Pagination } from '../components/table'
import { Badge, Button, Input, MultiSelect, Toast } from '../components/ui'

type Pr100Row = {
  revflow_patient_id: string
  dos: string
  primary_payer?: string | null
  primary_check_number?: string | null
  emr_patient_id?: string | null
  patient_name?: string | null
  facility_name?: string | null
  account_number?: string | null
}

type Meta = {
  filters: {
    facility: string[]
    month: string[]
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

const SORTABLE = new Set([
  'patient_name',
  'emr_patient_id',
  'account_number',
  'dos',
  'primary_payer',
  'facility_name',
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

function qs(params: Record<string, string | string[] | number | undefined>) {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v == null || v === '') continue
    if (Array.isArray(v)) v.forEach((x) => p.append(k, x))
    else p.set(k, String(v))
  }
  const s = p.toString()
  return s ? `?${s}` : ''
}

export function PatientResponsibilityPr100Tab() {
  const [q, setQ] = useState('')
  const [month, setMonth] = useState<string[]>([])
  const [facility, setFacility] = useState<string[]>([])
  const [sortBy, setSortBy] = useState('dos')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(100)
  const [items, setItems] = useState<Pr100Row[]>([])
  const [total, setTotal] = useState(0)
  const [pages, setPages] = useState(1)
  const [loading, setLoading] = useState(false)
  const [meta, setMeta] = useState<Meta | null>(null)
  const [toast, setToast] = useState<{ message: string; tone: 'info' | 'error' | 'success' } | null>(
    null,
  )

  const filterParams = useMemo(
    () => ({
      q: q || undefined,
      facility: facility.length ? facility : undefined,
      month: month.length ? month : undefined,
      sort_by: sortBy,
      sort_dir: sortDir,
      page,
      page_size: pageSize,
    }),
    [facility, month, page, pageSize, q, sortBy, sortDir],
  )

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await api<{ items: Pr100Row[]; total: number; pages: number }>(
        `/api/eligibility/pr100${qs(filterParams)}`,
      )
      setItems(data.items || [])
      setTotal(data.total || 0)
      setPages(data.pages || 1)
    } finally {
      setLoading(false)
    }
  }, [filterParams])

  useEffect(() => {
    void api<Meta>('/api/eligibility/pr-meta')
      .then(setMeta)
      .catch((e) => setToast({ message: String((e as Error).message || e), tone: 'error' }))
  }, [])

  useEffect(() => {
    void load().catch((e) => setToast({ message: String((e as Error).message || e), tone: 'error' }))
  }, [load])

  function toggleSort(key: string) {
    if (!SORTABLE.has(key)) return
    setPage(1)
    if (sortBy === key) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else {
      setSortBy(key)
      setSortDir(key === 'dos' ? 'desc' : 'asc')
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
    fetch(`/api/eligibility/pr100/export${qs(filterParams)}`, { credentials: 'include' })
      .then((r) => r.blob())
      .then((blob) => {
        const obj = URL.createObjectURL(blob)
        a.href = obj
        a.download = 'pr100.xlsx'
        a.click()
        URL.revokeObjectURL(obj)
      })
      .catch((e) => setToast({ message: String((e as Error).message || e), tone: 'error' }))
  }

  const months = useMemo(() => meta?.filters.month || [], [meta])

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      {toast && (
        <Toast message={toast.message} tone={toast.tone} onDismiss={() => setToast(null)} />
      )}
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-gray-500">{total.toLocaleString()} visits with PR-100</p>
        <Button variant="secondary" type="button" onClick={exportExcel}>
          <Download className="h-4 w-4" />
          Export Excel
        </Button>
      </div>
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900">
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-gray-200 px-3 py-2 dark:border-gray-800">
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
            value={month}
            onChange={(v) => {
              setPage(1)
              setMonth(v)
            }}
            options={months.map((m) => ({ value: m, label: monthLabel(m) }))}
            placeholder="All months"
            className="w-44"
          />
          <MultiSelect
            value={facility}
            onChange={(v) => {
              setPage(1)
              setFacility(v)
            }}
            options={(meta?.filters.facility || []).map((f) => ({ value: f, label: f }))}
            placeholder="All facilities"
            className="w-44"
          />
        </div>
        {items.length ? (
          <div className="table-scroll min-h-0 flex-1 overflow-auto">
            <table className="elig-sheet min-w-full text-left">
              <thead>
                <tr>
                  <th className="cursor-pointer" onClick={() => toggleSort('patient_name')}>
                    Patient {sortIcon('patient_name')}
                  </th>
                  <th className="cursor-pointer" onClick={() => toggleSort('emr_patient_id')}>
                    EMR {sortIcon('emr_patient_id')}
                  </th>
                  <th className="cursor-pointer" onClick={() => toggleSort('account_number')}>
                    Account # (PV4) {sortIcon('account_number')}
                  </th>
                  <th className="cursor-pointer" onClick={() => toggleSort('dos')}>
                    DOS {sortIcon('dos')}
                  </th>
                  <th className="cursor-pointer" onClick={() => toggleSort('primary_payer')}>
                    Insurance {sortIcon('primary_payer')}
                  </th>
                  <th>PR-100 Check #</th>
                  <th className="cursor-pointer" onClick={() => toggleSort('facility_name')}>
                    Facility {sortIcon('facility_name')}
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <tr key={`${row.revflow_patient_id}-${row.dos}`}>
                    <td className="font-medium text-gray-900 dark:text-white">
                      <span className="inline-flex flex-wrap items-center gap-1.5">
                        {row.patient_name || '—'}
                        {!row.emr_patient_id ? <Badge tone="amber">Needs EMR</Badge> : null}
                      </span>
                    </td>
                    <td className="text-gray-500">
                      <EmrPatientLink id={row.emr_patient_id} facilityName={row.facility_name} />
                    </td>
                    <td>
                      <WaystarAccountLink
                        accountNumber={row.account_number}
                        patientName={row.patient_name}
                      />
                    </td>
                    <td>{fmtDate(row.dos)}</td>
                    <td className="max-w-[180px] truncate" title={row.primary_payer || ''}>
                      {row.primary_payer || '—'}
                    </td>
                    <td>{row.primary_check_number || '—'}</td>
                    <td className="max-w-[180px] truncate" title={row.facility_name || ''}>
                      {row.facility_name || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="min-h-0 flex-1">
            <EmptyState
              title={loading ? 'Loading…' : 'No PR-100 visits'}
              description={
                loading
                  ? 'Fetching visits with PR-100 on an EOB check…'
                  : 'Visits with CARC PR-100 on an EOB check appear here.'
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
        />
      </div>
    </div>
  )
}
