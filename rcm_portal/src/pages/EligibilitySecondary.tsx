import { useCallback, useEffect, useMemo, useState } from 'react'
import { ArrowDown, ArrowUp, Download, Search } from 'lucide-react'
import { api } from '../api/client'
import { EmrPatientLink } from '../components/EmrPatientLink'
import { WaystarAccountLink } from '../components/WaystarAccountLink'
import { EmptyState, Pagination } from '../components/table'
import { Badge, Button, Input, MultiSelect, SearchableSelect, Toast } from '../components/ui'

type SecondaryRow = {
  revflow_patient_id: string
  dos: string
  primary_payer?: string | null
  primary_check_number?: string | null
  emr_patient_id?: string | null
  patient_name?: string | null
  facility_name?: string | null
  account_number?: string | null
  secondary_paid: boolean
  secondary_amount?: number | null
  secondary_check_number?: string | null
  secondary_check_date?: string | null
  secondary_payer?: string | null
  second_submission?: boolean
  second_insurance?: string | null
}

type Meta = {
  filters: {
    facility: string[]
    month: string[]
    second_insurance?: string[]
  }
}

const ADD_INSURANCE = '__add__'
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

function monthLabel(ym: string) {
  const [y, m] = ym.split('-')
  const idx = Number(m) - 1
  if (!y || Number.isNaN(idx) || idx < 0 || idx > 11) return ym
  return `${MONTH_NAMES[idx]}-${y}`
}

function fmtDate(d: string | null | undefined): string {
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

type QueueKind = 'pr2' | 'pr1'

const KIND = {
  pr2: {
    path: '/api/eligibility/secondary',
    exportName: 'secondary_payments.xlsx',
    checkCol: 'PR-2 Check #',
    statusCol: 'Secondary',
    codeLabel: 'PR-2',
    showPaid: true,
  },
  pr1: {
    path: '/api/eligibility/deductible',
    exportName: 'deductible_payments.xlsx',
    checkCol: 'PR-1 Check #',
    statusCol: 'Deductible',
    codeLabel: 'PR-1',
    showPaid: false,
  },
} as const

type Col = { key: string; label: string }

const SORTABLE = new Set([
  'patient_name',
  'emr_patient_id',
  'account_number',
  'dos',
  'primary_payer',
  'facility_name',
])

export function EligibilitySecondaryTab({ kind = 'pr2' }: { kind?: QueueKind }) {
  const cfg = KIND[kind]
  const columns: Col[] = cfg.showPaid
    ? [
        { key: 'patient_name', label: 'Patient' },
        { key: 'emr_patient_id', label: 'EMR' },
        { key: 'account_number', label: 'Account # (PV4)' },
        { key: 'dos', label: 'DOS' },
        { key: 'primary_payer', label: 'Insurance' },
        { key: 'second_insurance', label: 'Second Insurance' },
        { key: 'primary_check_number', label: cfg.checkCol },
        { key: 'second_submission', label: 'Second Submission' },
        { key: 'secondary_paid', label: cfg.statusCol },
        { key: 'secondary_amount', label: 'Paid Amount' },
        { key: 'secondary_check_number', label: 'Check #' },
        { key: 'secondary_check_date', label: 'Check Date' },
        { key: 'facility_name', label: 'Facility' },
      ]
    : [
        { key: 'patient_name', label: 'Patient' },
        { key: 'emr_patient_id', label: 'EMR' },
        { key: 'account_number', label: 'Account # (PV4)' },
        { key: 'dos', label: 'DOS' },
        { key: 'primary_payer', label: 'Insurance' },
        { key: 'second_insurance', label: 'Second Insurance' },
        { key: 'primary_check_number', label: cfg.checkCol },
        { key: 'second_submission', label: 'Second Submission' },
        { key: 'facility_name', label: 'Facility' },
      ]
  const [items, setItems] = useState<SecondaryRow[]>([])
  const [total, setTotal] = useState(0)
  const [paidCount, setPaidCount] = useState(0)
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(1)
  const [pageSize, setPageSize] = useState(100)
  const [q, setQ] = useState('')
  const [facility, setFacility] = useState<string[]>([])
  const [month, setMonth] = useState<string[]>([])
  const [paid, setPaid] = useState('all')
  const [sortBy, setSortBy] = useState('dos')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [savingKey, setSavingKey] = useState<string | null>(null)
  const [meta, setMeta] = useState<Meta | null>(null)
  const [extraInsurance, setExtraInsurance] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [toast, setToast] = useState<{ message: string; tone: 'info' | 'error' | 'success' } | null>(
    null,
  )

  const filterParams = useMemo(
    () => ({
      q: q || undefined,
      facility: facility.length ? facility : undefined,
      month: month.length ? month : undefined,
      paid: cfg.showPaid && paid !== 'all' ? paid : undefined,
      sort_by: sortBy,
      sort_dir: sortDir,
      page,
      page_size: pageSize,
    }),
    [q, facility, month, paid, page, pageSize, cfg.showPaid, sortBy, sortDir],
  )

  const loadList = useCallback(async () => {
    setLoading(true)
    try {
      const data = await api<{
        items: SecondaryRow[]
        total: number
        paid_count: number
        pages: number
      }>(`${cfg.path}${qs(filterParams)}`)
      setItems(data.items)
      setTotal(data.total)
      setPaidCount(data.paid_count || 0)
      setPages(data.pages || 1)
    } finally {
      setLoading(false)
    }
  }, [filterParams, cfg.path])

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

  function exportExcel() {
    const url = `${cfg.path}/export${qs({
      q: q || undefined,
      facility: facility.length ? facility : undefined,
      month: month.length ? month : undefined,
      paid: cfg.showPaid && paid !== 'all' ? paid : undefined,
      sort_by: sortBy,
      sort_dir: sortDir,
    })}`
    void fetch(url, { credentials: 'include' })
      .then((r) => r.blob())
      .then((blob) => {
        const obj = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = obj
        a.download = cfg.exportName
        a.click()
        URL.revokeObjectURL(obj)
      })
      .catch((e) => setToast({ message: String((e as Error).message || e), tone: 'error' }))
  }

  function toggleSort(key: string) {
    if (!SORTABLE.has(key)) return
    setPage(1)
    if (sortBy === key) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else {
      setSortBy(key)
      setSortDir(key === 'dos' ? 'desc' : 'asc')
    }
  }

  async function patchFlag(
    row: SecondaryRow,
    patch: { second_submission?: boolean; second_insurance?: string },
  ) {
    const key = `${row.revflow_patient_id}-${row.dos}`
    setSavingKey(key)
    const patientKey = row.emr_patient_id ? row.emr_patient_id : row.revflow_patient_id
    const nextSub = patch.second_submission ?? !!row.second_submission
    const nextIns = patch.second_insurance ?? row.second_insurance ?? ''

    if (
      'second_insurance' in patch &&
      (isNoSecondaryPayer(nextIns) || (kind === 'pr2' && isCollected(nextIns)))
    ) {
      setItems((cur) =>
        cur.filter((r) => (r.emr_patient_id || r.revflow_patient_id) !== patientKey),
      )
    } else if (
      'second_insurance' in patch &&
      kind === 'pr2' &&
      row.secondary_paid &&
      !isNoSecondaryPayer(nextIns)
    ) {
      const srcDos = String(row.dos || '').slice(0, 10)
      setItems((cur) =>
        cur
          .filter((r) => {
            const samePatient = (r.emr_patient_id || r.revflow_patient_id) === patientKey
            if (!samePatient) return true
            const dos = String(r.dos || '').slice(0, 10)
            if (r.revflow_patient_id === row.revflow_patient_id && dos === srcDos) return false
            if (dos > srcDos && r.secondary_paid) return false
            return true
          })
          .map((r) => {
            const samePatient = (r.emr_patient_id || r.revflow_patient_id) === patientKey
            if (!samePatient) return r
            return { ...r, second_insurance: patch.second_insurance ?? '' }
          }),
      )
    } else if ('second_insurance' in patch) {
      setItems((cur) =>
        cur.map((r) => {
          const samePatient = (r.emr_patient_id || r.revflow_patient_id) === patientKey
          if (!samePatient) return r
          return { ...r, second_insurance: patch.second_insurance ?? '' }
        }),
      )
    } else if ('second_submission' in patch && patch.second_submission) {
      // Mark siblings with insurance as about-to-move, then remove them
      setItems((cur) =>
        cur.filter((r) => {
          const samePatient = (r.emr_patient_id || r.revflow_patient_id) === patientKey
          if (!samePatient) return true
          // keep only rows that have neither submission nor insurance
          return !((r.second_submission ?? false) || (r.second_insurance || '').trim())
        }),
      )
    } else {
      // single-row optimistic update for other patches
      setItems((cur) =>
        cur.map((r) =>
          r.revflow_patient_id === row.revflow_patient_id && r.dos === row.dos
            ? { ...r, ...patch }
            : r,
        ),
      )
    }

    try {
      await api('/api/eligibility/pr-flags', {
        method: 'PATCH',
        body: JSON.stringify({
          revflow_patient_id: row.revflow_patient_id,
          dos: row.dos,
          kind,
          second_submission: nextSub,
          second_insurance: nextIns,
        }),
      })
      // Always reload — backend may have filled / promoted sibling rows
      await loadList()
    } catch (e) {
      await loadList()
      setToast({ message: String((e as Error).message || e), tone: 'error' })
    } finally {
      setSavingKey(null)
    }
  }

  async function toggleSecondSubmission(row: SecondaryRow, checked: boolean) {
    await patchFlag(row, { second_submission: checked })
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      {toast && (
        <Toast message={toast.message} tone={toast.tone} onDismiss={() => setToast(null)} />
      )}

      <div className="flex shrink-0 flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-gray-500">
          {cfg.showPaid
            ? `${total.toLocaleString()} visits with ${cfg.codeLabel} · ${paidCount.toLocaleString()} paid · ${(total - paidCount).toLocaleString()} not paid`
            : `${total.toLocaleString()} visits with ${cfg.codeLabel}`}
        </p>
        <Button variant="secondary" type="button" onClick={exportExcel}>
          <Download className="h-4 w-4" />
          Export Excel
        </Button>
      </div>

      <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900">
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-gray-200 px-3 py-2 dark:border-gray-800">
          <MultiSelect
            value={month}
            onChange={(v) => { setPage(1); setMonth(v) }}
            options={(meta?.filters.month || []).map((m) => ({ value: m, label: monthLabel(m) }))}
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
          {cfg.showPaid ? (
            <SearchableSelect
              value={paid}
              onChange={(v) => { setPage(1); setPaid(v) }}
              options={[
                { value: 'all', label: 'Paid + Not paid' },
                { value: 'paid', label: 'Paid only' },
                { value: 'unpaid', label: 'Not paid only' },
              ]}
              className="w-44"
            />
          ) : null}
        </div>

        {items.length ? (
          <div className="table-scroll min-h-0 flex-1 overflow-auto">
            <table className="elig-sheet min-w-full text-left">
              <thead>
                <tr>
                  {columns.map((col) => (
                    <th key={col.key}>
                      {SORTABLE.has(col.key) ? (
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
                      ) : (
                        col.label
                      )}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {items.map((row) => {
                  const rowKey = `${row.revflow_patient_id}-${row.dos}`
                  return (
                    <tr key={rowKey}>
                      <td className="font-medium text-gray-900 dark:text-white">
                        <span className="inline-flex flex-wrap items-center gap-1.5">
                          {row.patient_name || '—'}
                          {!row.emr_patient_id ? (
                            <Badge tone="amber">Needs EMR</Badge>
                          ) : null}
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
                      <td onClick={(e) => e.stopPropagation()}>
                        <SearchableSelect
                          value={row.second_insurance || ''}
                          disabled={savingKey === rowKey}
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
                              void patchFlag(row, { second_insurance: typed })
                              return
                            }
                            if (next === (row.second_insurance || '')) return
                            void patchFlag(row, { second_insurance: next })
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
                      <td>{row.primary_check_number || '—'}</td>
                      <td>
                        <input
                          type="checkbox"
                          className="h-4 w-4 accent-blue-600"
                          checked={!!row.second_submission}
                          disabled={savingKey === rowKey}
                          aria-label="Second Submission"
                          onChange={(e) => void toggleSecondSubmission(row, e.target.checked)}
                        />
                      </td>
                      {cfg.showPaid ? (
                        <>
                          <td>
                            <Badge tone={row.secondary_paid ? 'green' : 'gray'}>
                              {row.secondary_paid ? 'Paid' : 'Not Paid'}
                            </Badge>
                          </td>
                          <td className="tabular-nums">{money2(row.secondary_amount)}</td>
                          <td>{row.secondary_check_number || '—'}</td>
                          <td>{fmtDate(row.secondary_check_date)}</td>
                        </>
                      ) : null}
                      <td>{row.facility_name || '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="min-h-0 flex-1">
            <EmptyState
              title={loading ? 'Loading…' : 'No rows found'}
              description={
                loading
                  ? `Fetching ${cfg.codeLabel} visits…`
                  : `No visits with CARC ${cfg.codeLabel} match the current filters.`
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
