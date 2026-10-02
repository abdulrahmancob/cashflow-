import { useEffect, useMemo, useState } from 'react'
import {
  billingAnalysisApi,
  type BillingMonthRow,
  type BillingMonthly,
} from '../api/billingAnalysis'
import {
  EmptyState,
  Table,
  TableCard,
  Td,
  Th,
  THead,
  Tr,
} from '../components/table'
import { Alert, PageHeader, Select } from '../components/ui'

function money(n: number) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 0,
  }).format(Number(n) || 0)
}

function pct(n: number) {
  return `${(Number(n) || 0).toFixed(1)}%`
}

function currentYear() {
  return new Date().getFullYear()
}

const METRIC_COLS: Array<{
  key: keyof BillingMonthRow
  label: string
  className: string
  format?: (n: number) => string
}> = [
  { key: 'insurance_payment', label: 'Insurance payment', className: 'bg-amber-100 text-amber-900 dark:bg-amber-900/50 dark:text-amber-100', format: money },
  { key: 'copay', label: 'Copay / deductible', className: 'bg-sky-100 text-sky-900 dark:bg-sky-900/50 dark:text-sky-100', format: money },
  { key: 'total_payment', label: 'Total payment', className: 'bg-emerald-100 text-emerald-900 dark:bg-emerald-900/50 dark:text-emerald-100', format: money },
  { key: 'visits', label: 'Visits', className: 'bg-slate-100 text-slate-800 dark:bg-slate-800 dark:text-slate-100' },
  { key: 'ave_visit', label: 'Ave / visit', className: 'bg-slate-50 text-slate-800 dark:bg-slate-900 dark:text-slate-100', format: money },
  { key: 'paid_visits', label: 'Paid', className: 'bg-emerald-50 text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200' },
  { key: 'pending_visits', label: 'Pending', className: 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-100' },
  { key: 'denied_visits', label: 'Denied', className: 'bg-rose-100 text-rose-900 dark:bg-rose-900/50 dark:text-rose-100' },
  { key: 'payment_pct', label: 'Payment %', className: 'bg-indigo-100 text-indigo-900 dark:bg-indigo-900/50 dark:text-indigo-100', format: pct },
  { key: 'act_ave_visit', label: 'Act. ave / visit', className: 'bg-violet-100 text-violet-900 dark:bg-violet-900/50 dark:text-violet-100', format: money },
]

function MetricRow({ row, total = false }: { row: BillingMonthRow; total?: boolean }) {
  return (
    <Tr className={total ? 'bg-gray-50 font-semibold dark:bg-gray-800/60' : undefined}>
      <Td>{row.period}</Td>
      {METRIC_COLS.map((col) => {
        const n = Number(row[col.key] || 0)
        return (
          <Td key={col.key} className="tabular-nums">
            {col.format ? col.format(n) : n}
          </Td>
        )
      })}
    </Tr>
  )
}

function AgingRow({
  row,
  columns,
  total = false,
}: {
  row: BillingMonthRow
  columns: string[]
  total?: boolean
}) {
  return (
    <Tr className={total ? 'bg-gray-50 font-semibold dark:bg-gray-800/60' : undefined}>
      <Td>{total ? 'Total Cash flow' : row.period}</Td>
      <Td className="tabular-nums">{money(row.total_payment)}</Td>
      {columns.map((_, idx) => {
        const key = String(idx + 1).padStart(2, '0')
        return (
          <Td key={key} className="tabular-nums">
            {money(Number(row.collection?.[key] || 0))}
          </Td>
        )
      })}
    </Tr>
  )
}

export function BillingAnalysisPage() {
  const [year, setYear] = useState(currentYear())
  const [clinic, setClinic] = useState('')
  const [data, setData] = useState<BillingMonthly | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const years = useMemo(() => {
    const y = currentYear()
    return [y - 1, y, y + 1]
  }, [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    void billingAnalysisApi
      .monthly(year, clinic)
      .then((row) => {
        if (!cancelled) setData(row)
      })
      .catch((e) => {
        if (!cancelled) setError(String((e as Error).message))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [year, clinic])

  const agingCols = data?.aging_columns || []

  return (
    <div className="space-y-6">
      <PageHeader
        title="Billing cash flow"
        description="Primary billing by Date of Service. Insurance + copay from Snowflake visits — not Second Submission."
      />
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-sm text-gray-600 dark:text-gray-300">
          Year
          <Select
            className="mt-1 w-28"
            value={String(year)}
            onChange={(e) => setYear(Number(e.target.value))}
          >
            {years.map((y) => (
              <option key={y} value={y}>
                {y}
              </option>
            ))}
          </Select>
        </label>
        <label className="text-sm text-gray-600 dark:text-gray-300">
          Clinic
          <Select className="mt-1 w-56" value={clinic} onChange={(e) => setClinic(e.target.value)}>
            <option value="">All clinics</option>
            {(data?.clinics || []).map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </Select>
        </label>
      </div>
      {error && <Alert>{error}</Alert>}

      <TableCard
        title="Visit month"
        count={data?.rows.length}
        countLabel="months"
        description={
          loading
            ? 'Loading…'
            : clinic
              ? `Date of Service for ${clinic}.`
              : 'Date of Service for every clinic. Filter a clinic to match the Excel sheet.'
        }
      >
        {data?.rows.length ? (
          <div className="overflow-x-auto">
            <Table>
              <THead>
                <tr>
                  <Th>Period</Th>
                  {METRIC_COLS.map((col) => (
                    <Th key={col.key} className={col.className}>
                      {col.label}
                    </Th>
                  ))}
                </tr>
              </THead>
              <tbody>
                {data.rows.map((row) => (
                  <MetricRow key={row.period_start || row.period} row={row} />
                ))}
                <MetricRow row={data.totals} total />
              </tbody>
            </Table>
          </div>
        ) : (
          <EmptyState title={loading ? 'Loading analysis' : 'No Snowflake visits in this year'} />
        )}
      </TableCard>

      <TableCard
        title="Cash collected by month"
        countLabel="collection months"
        description="Visit month in rows. Collection month is Transaction Tracker, then Waystar, then Eligibility, then Snowflake. Amount is insurance + copay."
      >
        {data?.rows.length ? (
          <div className="overflow-x-auto">
            <Table>
              <THead>
                <tr>
                  <Th>Period</Th>
                  <Th className="bg-emerald-100 text-emerald-900 dark:bg-emerald-900/50 dark:text-emerald-100">
                    Total billing
                  </Th>
                  {agingCols.map((label) => (
                    <Th key={label}>{label}</Th>
                  ))}
                </tr>
              </THead>
              <tbody>
                {data.rows.map((row) => (
                  <AgingRow
                    key={`age-${row.period_start || row.period}`}
                    row={row}
                    columns={agingCols}
                  />
                ))}
                <AgingRow row={data.totals} columns={agingCols} total />
              </tbody>
            </Table>
          </div>
        ) : (
          <EmptyState title={loading ? 'Loading cash flow' : 'No collections in this year'} />
        )}
      </TableCard>
    </div>
  )
}
