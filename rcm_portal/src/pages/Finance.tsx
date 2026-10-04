import { useEffect, useMemo, useRef, useState } from 'react'
import { Navigate, useParams } from 'react-router-dom'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  forecastApi,
  money,
  monthLastDay,
  pct,
  sharePct,
  type Filters,
} from '../api/forecast'
import { cptAuditApi } from '../api/cptAudit'
import { useAuth } from '../auth/AuthContext'
import { EmrPatientLink } from '../components/EmrPatientLink'
import { FinanceFilters } from '../components/FinanceFilters'
import {
  EmptyState,
  Table,
  TableCard,
  Td,
  Th,
  THead,
  Tr,
} from '../components/table'
import { Alert, Button, Card, Input, KpiCard, PageHeader } from '../components/ui'

const empty: Filters = {
  facility: [],
  ins: [],
  stage: [],
  month: [],
  dateFrom: '',
  dateTo: '',
  severity: [],
  riskFlag: [],
  q: '',
}

const AUDIT_DOMAIN_LABEL: Record<string, string> = {
  cpt: 'CPT',
  icd: 'ICD-10',
  demo: 'Demographics',
  denied: 'Denied',
}

const INS_COLORS = [
  '#2563eb',
  '#0ea5e9',
  '#12b76a',
  '#f59e0b',
  '#ef4444',
  '#8b5cf6',
  '#ec4899',
  '#14b8a6',
]

type OutcomesSummary = Awaited<ReturnType<typeof forecastApi.outcomesSummary>>
type BehaviorTrend = Awaited<ReturnType<typeof forecastApi.behaviorTrend>>
type DailyPoint = {
  period: string
  amount: number
  horizon_kind?: string
  forecast_as_of?: string | null
}

function isoAddDays(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00`)
  if (Number.isNaN(d.getTime())) return iso
  d.setDate(d.getDate() + days)
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

function shortDay(period: string): string {
  const d = period.slice(5)
  return d || period
}

function PaidTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean
  payload?: Array<{
    name?: string
    value?: number | string
    color?: string
    dataKey?: string
    payload?: Record<string, unknown>
  }>
  label?: string
}) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-lg dark:border-gray-700 dark:bg-gray-900">
      {label && <div className="mb-1 text-xs font-medium text-gray-500">{label}</div>}
      {payload.map((p, i) => {
        const n = Number(p.value ?? 0)
        const row = p.payload || {}
        const countKey = `${p.dataKey || p.name || ''}__n`
        const count = Number(row[countKey] ?? 0)
        return (
          <div key={p.dataKey || p.name || i} className="flex items-center gap-2 text-sm">
            <span className="h-2 w-2 rounded-full" style={{ background: p.color }} />
            <span className="text-gray-500">{p.name || p.dataKey}</span>
            <span className="font-semibold text-gray-900 dark:text-white">{money(n)}</span>
            {count > 0 && (
              <span className="text-xs text-gray-400">
                {count} check{count === 1 ? '' : 's'}
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}

function MoneyTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean
  payload?: Array<{
    name?: string
    value?: number | string
    color?: string
    dataKey?: string
    payload?: Record<string, unknown>
  }>
  label?: string
}) {
  if (!active || !payload?.length) return null
  const asOf = payload[0]?.payload?.forecast_as_of
  return (
    <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-lg dark:border-gray-700 dark:bg-gray-900">
      {label && <div className="mb-1 text-xs font-medium text-gray-500">{label}</div>}
      {asOf != null && asOf !== '' && (
        <div className="mb-1 text-xs text-gray-400">Closest forecast from {String(asOf)}</div>
      )}
      {payload.map((p, i) => {
        if (p.value == null || p.value === '') return null
        const row = p.payload || {}
        const share = row.share_pct
        const n = Number(p.value)
        const isDays = String(p.name || p.dataKey || '').toLowerCase().includes('lag')
        const shown = isDays
          ? `${n.toFixed(1)} days`
          : Number.isFinite(n) && Math.abs(n) < 200 && String(p.dataKey || '').includes('share')
            ? sharePct(n)
            : money(n)
        return (
          <div key={p.dataKey || p.name || i} className="flex items-center gap-2 text-sm">
            <span className="h-2 w-2 rounded-full" style={{ background: p.color }} />
            <span className="text-gray-500">{p.name || p.dataKey}</span>
            <span className="font-semibold text-gray-900 dark:text-white">{shown}</span>
            {share != null && !isDays && (
              <span className="text-xs text-gray-400">{sharePct(share)}</span>
            )}
          </div>
        )
      })}
    </div>
  )
}

const MIX_SEGMENTS = [
  { key: 'landed' as const, label: 'In (sheet)', bar: '#12b76a', chip: 'text-emerald-700 dark:text-emerald-400' },
  { key: 'overdue' as const, label: 'Overdue', bar: '#f59e0b', chip: 'text-amber-700 dark:text-amber-400' },
  { key: 'risk' as const, label: 'Risk', bar: '#e11d48', chip: 'text-rose-700 dark:text-rose-400' },
]

function InsuranceMixList({
  rows,
}: {
  rows: Array<{ ins_name: string; landed: number; overdue: number; risk: number }>
}) {
  const totals = {
    landed: rows.reduce((s, r) => s + r.landed, 0),
    overdue: rows.reduce((s, r) => s + r.overdue, 0),
    risk: rows.reduce((s, r) => s + r.risk, 0),
  }
  const grand = totals.landed + totals.overdue + totals.risk

  const amountCols = (row: { landed: number; overdue: number; risk: number }, strong?: boolean) => (
    <div className="flex w-64 shrink-0 justify-end gap-2.5 tabular-nums">
      {MIX_SEGMENTS.map((s) => (
        <span
          key={s.key}
          className={`w-[5.25rem] text-right text-[11px] ${strong ? 'font-semibold' : 'font-medium'} ${s.chip}`}
        >
          {money(row[s.key])}
        </span>
      ))}
    </div>
  )

  const bar = (row: { landed: number; overdue: number; risk: number }, total: number) => (
    <div className="flex h-2.5 min-w-0 flex-1 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
      {MIX_SEGMENTS.map((s) => {
        const amt = row[s.key]
        if (amt <= 0) return null
        return (
          <div
            key={s.key}
            title={`${s.label}: ${money(amt)}`}
            className="h-full"
            style={{
              background: s.bar,
              flexGrow: total > 0 ? amt : 1,
              minWidth: 3,
            }}
          />
        )
      })}
    </div>
  )

  return (
    <div className="px-4 pb-3 pt-2">
      <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-500">
        {MIX_SEGMENTS.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full" style={{ background: s.bar }} />
            {s.label}
          </span>
        ))}
      </div>
      <div className="max-h-96 space-y-2.5 overflow-y-auto pr-1">
        {rows.map((row) => {
          const total = row.landed + row.overdue + row.risk
          return (
            <div key={row.ins_name} className="flex items-center gap-3">
              <div
                className="w-36 shrink-0 truncate text-xs font-medium text-gray-700 dark:text-gray-300"
                title={row.ins_name}
              >
                {row.ins_name}
              </div>
              {bar(row, total)}
              {amountCols(row)}
            </div>
          )
        })}
      </div>
      <div className="mt-3 flex items-center gap-3 border-t border-gray-200 pt-3 dark:border-gray-700">
        <div className="w-36 shrink-0 text-xs font-semibold text-gray-900 dark:text-white">Total</div>
        {bar(totals, grand)}
        {amountCols(totals, true)}
      </div>
    </div>
  )
}

function buildCfoInsightCards(
  summary: OutcomesSummary | null,
  kpi: Record<string, number | string | boolean>,
): Array<{ title: string; body: string }> {
  const mix = [...(summary?.insurance_mix ?? [])].map((r) => ({
    ins_name: String(r.ins_name || '(blank)'),
    landed: Number(r.landed || 0),
    overdue: Number(r.overdue || 0),
    risk: Number(r.risk || 0),
  }))
  const overdueRows = [...(summary?.overdue_by_insurance ?? [])].sort(
    (a, b) => Number(b.expected_payment || 0) - Number(a.expected_payment || 0),
  )
  const flagRows = [...(summary?.risk_by_flag ?? [])]
    .map((r) => ({
      label: AUDIT_DOMAIN_LABEL[String(r.risk_flag || '')] || String(r.risk_flag || '').replace(/_/g, ' '),
      amount: Number(r.exposure_amount || 0),
    }))
    .sort((a, b) => b.amount - a.amount)

  const landedTotal = mix.reduce((s, r) => s + r.landed, 0)
  const topIn = [...mix].sort((a, b) => b.landed - a.landed)[0]
  const overdueTotal = Number(kpi.overdue_amount || 0) || overdueRows.reduce((s, r) => s + Number(r.expected_payment || 0), 0)
  const topOd = overdueRows[0]
  const riskTotal = Number(kpi.risk_exposure_amount || 0) || flagRows.reduce((s, r) => s + r.amount, 0)
  const riskVisits = Number(kpi.risk_visit_count || 0)
  const topFlag = flagRows[0]

  const twoStory = [...mix]
    .filter((r) => r.landed > 0 && r.overdue + r.risk > 0)
    .sort((a, b) => b.landed - a.landed)[0]
  const littleIn = [...mix]
    .filter((r) => r.overdue + r.risk > 0 && r.landed < (r.overdue + r.risk) * 0.25)
    .filter((r) => r.ins_name !== twoStory?.ins_name)
    .sort((a, b) => b.overdue + b.risk - (a.overdue + a.risk))[0]

  const cards: Array<{ title: string; body: string }> = []
  if (landedTotal > 0 && topIn) {
    cards.push({
      title: 'In',
      body: `Eligibility Sheet paid ${money(landedTotal)} in this window. Largest: ${topIn.ins_name} ${money(topIn.landed)}.`,
    })
  }
  if (overdueTotal > 0) {
    cards.push({
      title: 'Overdue',
      body: topOd
        ? `Open AR past land is ${money(overdueTotal)}. ${topOd.ins_name} is the largest at ${money(topOd.expected_payment)}.`
        : `Open AR past land is ${money(overdueTotal)}.`,
    })
  }
  {
    const visits =
      riskTotal > 0 && Number.isFinite(riskVisits) && riskVisits > 0
        ? ` on ${riskVisits.toLocaleString()} visits`
        : ''
    const mostly = riskTotal > 0 && topFlag?.label ? ` Mostly ${topFlag.label}.` : ''
    cards.push({
      title: 'Risk',
      body: `Open audit-queue risk is ${money(riskTotal)}${visits} (Eligibility Sheet paid status excluded).${mostly}`,
    })
  }
  if (twoStory) {
    cards.push({
      title: 'Same payer, two stories',
      body: `${twoStory.ins_name} brought in ${money(twoStory.landed)} and still has ${money(twoStory.overdue)} overdue / ${money(twoStory.risk)} risk.`,
    })
  }
  if (littleIn) {
    cards.push({
      title: 'Out with little in',
      body: `${littleIn.ins_name} has ${money(littleIn.overdue)} overdue / ${money(littleIn.risk)} risk and little sheet-paid in this window.`,
    })
  }
  return cards
}

export function FinancePage() {
  const { tab } = useParams()
  const { hasRole } = useAuth()
  const view = tab || 'cash'
  const [filters, setFilters] = useState<Filters>(empty)
  const [behaviorView, setBehaviorView] = useState<'chart' | 'checks'>('chart')
  const [kpi, setKpi] = useState<Record<string, number | string | boolean>>({})
  const [monthly, setMonthly] = useState<Array<{ period: string; amount: number }>>([])
  const [daily, setDaily] = useState<DailyPoint[]>([])
  const [actual, setActual] = useState<DailyPoint[]>([])
  const [lastSettled, setLastSettled] = useState('')
  const [summary, setSummary] = useState<OutcomesSummary | null>(null)
  const [behavior, setBehavior] = useState<BehaviorTrend | null>(null)
  const [cptInsights, setCptInsights] = useState<{
    insights_published: boolean
    headline: Record<string, number | null>
    secondary: Record<string, number>
    missing_90901_by_month: Array<Record<string, unknown>>
  } | null>(null)
  const [overdue, setOverdue] = useState<
    Awaited<ReturnType<typeof forecastApi.overdueClaims>>
  >([])
  const [error, setError] = useState('')
  const [missionLoading, setMissionLoading] = useState(view === 'mission')
  const [dayAhead, setDayAhead] = useState<{
    yesterday: {
      bank_date: string
      forecast_total: number
      actual_total: number
      error_pct: number | null
    } | null
    recent: Array<{ bank_date: string; error_pct: number | null }>
  } | null>(null)
  const [checkRows, setCheckRows] = useState<
    NonNullable<BehaviorTrend>['checks']
  >([])
  const [checksLoading, setChecksLoading] = useState(false)
  const filterSig = useMemo(() => JSON.stringify(filters), [filters])
  const prevFilterSig = useRef(filterSig)
  const missionPainted = useRef(false)
  const checksLoadedFor = useRef('')

  const splitFilters = filters.facility.length > 0 || filters.ins.length > 0

  useEffect(() => {
    let cancelled = false
    const filtersChanged = prevFilterSig.current !== filterSig
    prevFilterSig.current = filterSig
    const delay = filtersChanged ? 250 : 0
    if (filtersChanged) {
      checksLoadedFor.current = ''
      setCheckRows([])
    }
    const handle = window.setTimeout(() => {
      ;(async () => {
        try {
          setError('')
          if (view === 'mission') {
            if (!missionPainted.current) setMissionLoading(true)
            const bundle = await forecastApi.mission(filters)
            if (!cancelled) {
              setKpi(bundle.kpi)
              setSummary(bundle.outcomes)
              setBehavior(bundle.behavior)
              setDayAhead(bundle.day_ahead || null)
              missionPainted.current = true
              setMissionLoading(false)
            }
          } else if (view === 'cash') {
            const [hist, a, meta] = await Promise.all([
              forecastApi.projectedHistory(filters),
              forecastApi.actualDaily(filters),
              forecastApi.filters(),
            ])
            if (!cancelled) {
              setDaily(hist.daily)
              setActual(a)
              setMonthly(hist.monthly)
              setLastSettled(meta.last_settled_date || '')
            }
          } else if (view === 'insights') {
            const [cpt, outcomes, k] = await Promise.all([
              cptAuditApi.insights().catch(() => null),
              forecastApi.outcomesSummary(filters).catch(() => null),
              forecastApi.kpi(filters).catch(() => ({})),
            ])
            if (!cancelled) {
              setCptInsights(cpt)
              setSummary(outcomes)
              setKpi(k)
            }
          } else if (view === 'overdue') {
            const rows = await forecastApi.overdueClaims(filters)
            if (!cancelled) setOverdue(rows)
          }
        } catch (e) {
          if (!cancelled) {
            setError(String((e as Error).message || e))
            if (view === 'mission') setMissionLoading(false)
          }
        }
      })()
    }, delay)
    return () => {
      cancelled = true
      window.clearTimeout(handle)
    }
  }, [view, filters, filterSig])

  useEffect(() => {
    if (view !== 'mission' || behaviorView !== 'checks') return
    if (checksLoadedFor.current === filterSig) return
    let cancelled = false
    checksLoadedFor.current = filterSig
    setChecksLoading(true)
    ;(async () => {
      try {
        const payload = await forecastApi.behaviorTrend(filters, 'day')
        if (!cancelled) setCheckRows(payload.checks || [])
      } catch {
        if (!cancelled) setCheckRows([])
      } finally {
        if (!cancelled) setChecksLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [view, behaviorView, filters, filterSig])

  const settled = lastSettled || actual.reduce((m, r) => (r.period > m ? r.period : m), '')

  const monthlyCompare = useMemo(() => {
    const landed = new Map<string, number>()
    for (const r of actual) {
      const ym = r.period.slice(0, 7)
      if (ym.length < 7) continue
      landed.set(ym, (landed.get(ym) || 0) + Number(r.amount || 0))
    }
    const periods = new Set<string>()
    for (const r of monthly) periods.add(r.period)
    for (const ym of landed.keys()) periods.add(ym)
    return [...periods]
      .sort()
      .filter((ym) => !settled || monthLastDay(ym) <= settled)
      .map((period) => ({
        period,
        landed: Math.round((landed.get(period) || 0) * 100) / 100,
        expected: Math.round((monthly.find((r) => r.period === period)?.amount || 0) * 100) / 100,
      }))
      .filter((r) => r.expected > 0)
  }, [actual, monthly, settled])

  const varianceRows = useMemo(
    () =>
      monthlyCompare.map((r) => ({
        ...r,
        variance: Math.round((r.landed - r.expected) * 100) / 100,
      })),
    [monthlyCompare],
  )

  const cashSeries = useMemo(() => {
    const map = new Map<
      string,
      {
        period: string
        projected: number
        actual: number | null
        forecast_as_of?: string | null
      }
    >()
    for (const r of daily) {
      map.set(r.period, {
        period: r.period,
        projected: r.amount,
        actual: null,
        forecast_as_of: r.forecast_as_of,
      })
    }
    for (const r of actual) {
      if (settled && r.period > settled) continue
      const cur = map.get(r.period) || { period: r.period, projected: 0, actual: null }
      cur.actual = r.amount
      map.set(r.period, cur)
    }
    return [...map.values()].sort((a, b) => a.period.localeCompare(b.period)).slice(-60)
  }, [daily, actual, settled])

  const horizonRows = useMemo(() => {
    if (!settled) return []
    const start = isoAddDays(settled, 1)
    const end = isoAddDays(settled, 10)
    const proj = new Map(daily.map((r) => [r.period, Number(r.amount || 0)]))
    const act = new Map(actual.map((r) => [r.period, Number(r.amount || 0)]))
    const rows: Array<{
      period: string
      projected: number
      actual: number | null
      horizon_kind?: string
    }> = []
    for (let i = 0; i < 10; i += 1) {
      const period = isoAddDays(start, i)
      if (period > end) break
      const kind = daily.find((r) => r.period === period)?.horizon_kind
      rows.push({
        period,
        projected: proj.get(period) || 0,
        actual: settled && period <= settled ? (act.get(period) ?? null) : null,
        horizon_kind: kind,
      })
    }
    return rows
  }, [daily, actual, settled])

  const overdueRows = useMemo(
    () =>
      [...(summary?.overdue_by_insurance ?? [])].sort(
        (a, b) => Number(b.expected_payment) - Number(a.expected_payment),
      ),
    [summary],
  )
  const riskRows = useMemo(
    () =>
      [...(summary?.risk_by_insurance ?? [])].sort(
        (a, b) => Number(b.exposure_amount) - Number(a.exposure_amount),
      ),
    [summary],
  )
  const mixRows = useMemo(
    () =>
      [...(summary?.insurance_mix ?? [])]
        .map((r) => ({
          ins_name: r.ins_name,
          landed: Number(r.landed || 0),
          overdue: Number(r.overdue || 0),
          risk: Number(r.risk || 0),
        }))
        .sort((a, b) => b.landed + b.overdue + b.risk - (a.landed + a.overdue + a.risk)),
    [summary],
  )
  const stageRows = useMemo(
    () =>
      (summary?.stages ?? []).map((s) => ({
        ...s,
        label: String(s.outcome_stage || '').replace(/_/g, ' '),
      })),
    [summary],
  )
  const riskByFlag = useMemo(
    () =>
      [...(summary?.risk_by_flag ?? [])]
        .map((r) => {
          const flag = String(r.risk_flag || '')
          return {
            label: AUDIT_DOMAIN_LABEL[flag] || flag.replace(/_/g, ' ') || '(blank)',
            exposure_amount: Number(r.exposure_amount || 0),
          }
        })
        .sort((a, b) => b.exposure_amount - a.exposure_amount)
        .slice(0, 12),
    [summary],
  )
  const cptRiskBars = useMemo(() => {
    if (!cptInsights?.insights_published) return []
    return [
      { label: 'At-risk paid', amount: Number(cptInsights.headline.at_risk_paid || 0) },
      { label: 'Open at-risk', amount: Number(cptInsights.headline.open_at_risk || 0) },
    ]
  }, [cptInsights])

  const behaviorChart = useMemo(() => {
    const series = behavior?.series ?? []
    const names = behavior?.insurers?.length
      ? behavior.insurers
      : [...new Set(series.map((r) => r.ins_name))]
    const top = names.slice(0, 2)
    const byDay = new Map<string, Record<string, string | number | null>>()
    for (const r of series) {
      if (!top.includes(r.ins_name)) continue
      const day = r.period
      if (!day) continue
      const row = byDay.get(day) || { period: day }
      row[r.ins_name] = Number(row[r.ins_name] || 0) + Number(r.paid_amount || 0)
      row[`${r.ins_name}__n`] =
        Number(row[`${r.ins_name}__n`] || 0) + Number(r.check_count || 0)
      byDay.set(day, row)
    }
    const points = [...byDay.values()].sort((a, b) =>
      String(a.period).localeCompare(String(b.period)),
    )
    return { top, points }
  }, [behavior])
  const cfoCards = useMemo(() => buildCfoInsightCards(summary, kpi), [summary, kpi])

  const arOpen = Number(kpi.on_track_amount ?? 0) + Number(kpi.overdue_amount ?? 0)
  const riskAmt = Number(kpi.risk_exposure_amount ?? 0)
  const bankHint = 'Tracker cash is company-wide — not split by clinic or insurance'

  const titles: Record<string, string> = {
    mission: 'Mission Control',
    cash: 'Cash Trajectory',
    insights: 'Business Insights',
    overdue: 'Overdue',
    drill: 'Overdue',
  }

  if (tab === 'exec') return <Navigate to="/finance/cash" replace />
  if (tab === 'drill') return <Navigate to="/finance/overdue" replace />

  return (
    <div className="space-y-6">
      <PageHeader
        title={titles[view] || 'Finance'}
        description="Read-only finance dashboards in Remitarc."
      />
      {error && <Alert>{error}</Alert>}

      <FinanceFilters filters={filters} onChange={setFilters} />

      {view === 'mission' && missionLoading && (
        <div className="flex min-h-[24rem] items-center justify-center">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-gray-200 border-t-brand-600" />
        </div>
      )}
      {view === 'mission' && !missionLoading && dayAhead?.yesterday && (
        <div className="mb-4 grid gap-4 sm:grid-cols-2">
          <KpiCard
            label={`Day-ahead ${dayAhead.yesterday.bank_date}`}
            value={money(dayAhead.yesterday.forecast_total)}
            tone="info"
            hint={`Tracker ${money(dayAhead.yesterday.actual_total)} · error ${
              dayAhead.yesterday.error_pct == null ? '—' : `${dayAhead.yesterday.error_pct}%`
            }`}
          />
          <KpiCard
            label="Last 5 bank days"
            value={
              dayAhead.recent
                .map((row) => (row.error_pct == null ? '—' : `${row.error_pct}%`))
                .join(' · ') || '—'
            }
            hint="Day-ahead error vs the frozen tracker"
          />
        </div>
      )}
      {view === 'mission' && !missionLoading && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <KpiCard
              label={
                filters.dateFrom || filters.dateTo || filters.month.length
                  ? 'Past projected'
                  : 'Projected AR'
              }
              value={money(kpi.projected_cash_in)}
              tone="info"
              hint={
                filters.dateFrom || filters.dateTo || filters.month.length
                  ? kpi.closest_forecast_as_of
                    ? `Forecast from ${String(kpi.closest_forecast_as_of)}`
                    : 'Same as Cash Trajectory daily expected'
                  : undefined
              }
            />
            <KpiCard
              label={
                filters.dateFrom || filters.dateTo || filters.month.length
                  ? 'Actual in range'
                  : 'Actual received'
              }
              value={money(kpi.actual_cash_received_filtered ?? kpi.actual_cash_received)}
              tone="ok"
              hint={[
                'Tracker txn_date',
                Number(kpi.actual_line_count ?? 0)
                  ? `${Number(kpi.actual_line_count).toLocaleString()} row${Number(kpi.actual_line_count) === 1 ? '' : 's'}`
                  : null,
                'not clinic-split',
              ]
                .filter(Boolean)
                .join(' · ')}
            />
            {(filters.dateFrom || filters.dateTo || filters.month.length > 0) &&
              kpi.variance_amount != null &&
              kpi.variance_amount !== '' && (
              <KpiCard
                label="Variance (actual − projected)"
                value={money(kpi.variance_amount)}
                tone={Number(kpi.variance_amount ?? 0) >= 0 ? 'ok' : 'warn'}
              />
            )}
            <KpiCard
              label="On track"
              value={money(kpi.on_track_amount)}
              hint={`${pct(kpi.on_track_amount, arOpen)} of open AR`}
            />
            <KpiCard
              label="Overdue"
              value={money(kpi.overdue_amount)}
              tone="warn"
              hint={`${pct(kpi.overdue_amount, arOpen)} of open AR`}
            />
            <KpiCard
              label="At-risk exposure"
              value={money(riskAmt)}
              tone="danger"
              hint="Open CPT / ICD-10 / Demographics queue risk plus Collection Denied charged — not warnings"
            />
          </div>
          {(filters.dateFrom || filters.dateTo || filters.month.length > 0) && (
            <p className="text-xs text-gray-400">
              On track = visits in this range still inside SLA. Overdue = expected to
              land in this range and still late.
            </p>
          )}

          <Card
            title="Insurance checks"
            description={
              filters.ins.length
                ? `Each point is a Tracker deposit day for ${filters.ins.join(', ')}`
                : filters.dateFrom || filters.dateTo || filters.month.length
                  ? 'Each point is a Tracker deposit day (top 2 drop inside the selected dates)'
                  : 'Each point is a Tracker deposit day (top 2 drop across Tracker history)'
            }
            action={
              <div className="flex gap-1">
                <Button
                  type="button"
                  size="sm"
                  variant={behaviorView === 'chart' ? 'primary' : 'secondary'}
                  onClick={() => setBehaviorView('chart')}
                >
                  Chart
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant={behaviorView === 'checks' ? 'primary' : 'secondary'}
                  onClick={() => setBehaviorView('checks')}
                >
                  Checks
                </Button>
              </div>
            }
            padded={false}
          >
            {behaviorView === 'checks' ? (
              checksLoading ? (
                <div className="flex h-48 items-center justify-center">
                  <div className="h-8 w-8 animate-spin rounded-full border-2 border-gray-200 border-t-brand-600" />
                </div>
              ) : checkRows.length ? (
                <div className="max-h-96 overflow-y-auto">
                <Table>
                  <THead>
                    <Tr>
                      <Th>Date</Th>
                      <Th>Insurance</Th>
                      <Th>Check #</Th>
                      <Th className="text-right">Amount</Th>
                    </Tr>
                  </THead>
                  <tbody>
                    {checkRows.map((r, i) => (
                      <Tr key={`${r.txn_date}-${r.check_num}-${i}`}>
                        <Td>{r.txn_date}</Td>
                        <Td>{r.ins_name || '(blank)'}</Td>
                        <Td>{r.check_num || '—'}</Td>
                        <Td className="text-right">{money(r.paid_amount)}</Td>
                      </Tr>
                    ))}
                  </tbody>
                </Table>
                </div>
              ) : (
                <div className="flex h-48 items-center justify-center px-5">
                  <EmptyState
                    title="No tracker checks"
                    description="No Transaction Tracker deposits for the current insurance or dates."
                  />
                </div>
              )
            ) : (
              <div className="h-72 px-2 pb-3 pt-2">
                {behaviorChart.points.length ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={behaviorChart.points}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
                      <XAxis
                        dataKey="period"
                        tick={{ fill: '#667085', fontSize: 12 }}
                        axisLine={false}
                        tickLine={false}
                        tickFormatter={(v) => (String(v).length >= 10 ? String(v).slice(5) : String(v))}
                      />
                      <YAxis tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
                      <Tooltip content={<PaidTooltip />} />
                      <Legend />
                      {behaviorChart.top.map((name, i) => (
                        <Line
                          key={name}
                          type="monotone"
                          dataKey={name}
                          name={name}
                          stroke={INS_COLORS[i % INS_COLORS.length]}
                          strokeWidth={2}
                          dot={{ r: 3 }}
                          connectNulls
                        />
                      ))}
                    </LineChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="flex h-full items-center justify-center px-5">
                    <EmptyState
                      title="No tracker cash"
                      description="No Transaction Tracker deposits for the current insurance or dates."
                    />
                  </div>
                )}
              </div>
            )}
          </Card>

          <Card
            title="Insurance in / overdue / risk"
            description="In = Eligibility Sheet paid. Overdue = forecast land lag. Risk = open audit queue minus Eligibility Sheet paid status."
            padded={false}
          >
            {mixRows.length ? (
              <InsuranceMixList rows={mixRows} />
            ) : (
              <div className="flex h-64 items-center justify-center px-5">
                <EmptyState
                  title="No insurance mix"
                  description="No Eligibility Sheet paid, overdue, or open audit-queue risk for the current filters."
                />
              </div>
            )}
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="Overdue by insurance" description="Open AR past expected land date" padded={false}>
              <div className="h-72 px-2 pb-3 pt-2">
                {overdueRows.length ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={overdueRows.slice(0, 12)} layout="vertical" barCategoryGap="18%">
                      <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" horizontal={false} />
                      <XAxis type="number" tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
                      <YAxis type="category" dataKey="ins_name" width={120} tick={{ fill: '#667085', fontSize: 11 }} axisLine={false} tickLine={false} />
                      <Tooltip content={<MoneyTooltip />} />
                      <Bar dataKey="expected_payment" name="Overdue" fill="#f59e0b" radius={[0, 6, 6, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="flex h-full items-center justify-center px-5">
                    <EmptyState title="No overdue" description="No overdue insurance rows for the current filters." />
                  </div>
                )}
              </div>
            </Card>

            <Card
              title="At-risk by insurance"
              description="Open CPT / ICD-10 / Demographics audit risk plus Collection Denied charged — not warnings, not payment lag"
              padded={false}
            >
              <div className="h-72 px-2 pb-3 pt-2">
                {riskRows.length ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={riskRows.slice(0, 12)} layout="vertical" barCategoryGap="18%">
                      <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" horizontal={false} />
                      <XAxis type="number" tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
                      <YAxis type="category" dataKey="ins_name" width={120} tick={{ fill: '#667085', fontSize: 11 }} axisLine={false} tickLine={false} />
                      <Tooltip content={<MoneyTooltip />} />
                      <Bar dataKey="exposure_amount" name="At-risk" fill="#e11d48" radius={[0, 6, 6, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="flex h-full items-center justify-center px-5">
                    <EmptyState
                      title="No open audit-queue risk"
                      description="No open CPT / ICD-10 / Demographics risk items for the current filters."
                    />
                  </div>
                )}
              </div>
            </Card>
          </div>

          <Card title="Outcome mix" description="Expected dollars by stage (forecast / open AR)" padded={false}>
            <div className="h-72 px-2 pb-3 pt-2">
              {stageRows.length ? (
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={stageRows} barCategoryGap="22%">
                    <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
                    <XAxis dataKey="label" tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
                    <YAxis tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
                    <Tooltip content={<MoneyTooltip />} />
                    <Bar dataKey="amount" name="Amount" fill="#2563eb" radius={[6, 6, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-full items-center justify-center px-5">
                  <EmptyState title="No stages" description="No outcome mix for the current filters." />
                </div>
              )}
            </div>
          </Card>
        </>
      )}

      {view === 'cash' && (
        <div className="space-y-4">
          {splitFilters && (
            <p className="text-xs text-gray-400">{bankHint}. Monthly landed vs expected is hidden while clinic or insurance is selected.</p>
          )}
          {!splitFilters && (
            <div className="grid gap-4 lg:grid-cols-2">
              <Card
                title="Landed vs expected"
                description="Tracker cash vs closest prior forecast. Settled months only."
                padded={false}
              >
                <div className="h-72 px-2 pb-3 pt-2">
                  {monthlyCompare.length ? (
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={monthlyCompare} barCategoryGap="18%">
                        <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
                        <XAxis dataKey="period" tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
                        <YAxis tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
                        <Tooltip content={<MoneyTooltip />} />
                        <Legend />
                        <Bar dataKey="landed" name="Tracker cash" fill="#12b76a" radius={[4, 4, 0, 0]} />
                        <Bar dataKey="expected" name="Expected (closest prior forecast)" fill="#2563eb" radius={[4, 4, 0, 0]} />
                      </BarChart>
                    </ResponsiveContainer>
                  ) : (
                    <div className="flex h-full items-center justify-center px-5">
                      <EmptyState title="No settled months" description="No Tracker cash or expected dollars for settled months." />
                    </div>
                  )}
                </div>
              </Card>
              <Card
                title="Variance by month"
                description="Tracker cash minus closest prior forecast. Green beat, red miss. Incomplete months omitted."
                padded={false}
              >
                <div className="h-72 px-2 pb-3 pt-2">
                  {varianceRows.length ? (
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={varianceRows} barCategoryGap="22%">
                        <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
                        <XAxis dataKey="period" tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
                        <YAxis tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
                        <Tooltip content={<MoneyTooltip />} />
                        <Bar dataKey="variance" name="Variance" radius={[4, 4, 0, 0]}>
                          {varianceRows.map((r) => (
                            <Cell key={r.period} fill={r.variance >= 0 ? '#12b76a' : '#ef4444'} />
                          ))}
                        </Bar>
                      </BarChart>
                    </ResponsiveContainer>
                  ) : (
                    <div className="flex h-full items-center justify-center px-5">
                      <EmptyState title="No variance" description="Need settled months with both Tracker cash and expected." />
                    </div>
                  )}
                </div>
              </Card>
            </div>
          )}

          <Card
            title="Projected vs actual (daily)"
            description={`Last 60 days. Tracker actual stops at ${settled || 'last settled bank date'}. ${bankHint}.`}
            padded={false}
          >
            <div className="h-80 px-2 pb-3 pt-2">
              {cashSeries.length ? (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={cashSeries}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
                    <XAxis dataKey="period" tick={{ fill: '#667085', fontSize: 11 }} axisLine={false} tickLine={false} />
                    <YAxis tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
                    <Tooltip content={<MoneyTooltip />} />
                    <Legend />
                    <Line type="monotone" dataKey="projected" name="Expected (closest prior forecast)" stroke="#2563eb" strokeWidth={2} dot={false} />
                    <Line type="monotone" dataKey="actual" name="Tracker cash" stroke="#12b76a" strokeWidth={2} dot={false} connectNulls={false} />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-full items-center justify-center px-5">
                  <EmptyState title="No daily cash" description="No projected or Tracker cash for the current dates." />
                </div>
              )}
            </div>
          </Card>

          <Card
            title="Next 10 days expected"
            description="Current forecast after the last settled bank day. No Tracker actual yet — that is pending entry, not a miss."
            padded={false}
          >
            <div className="h-72 px-2 pb-3 pt-2">
              {horizonRows.length ? (
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={horizonRows}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
                    <XAxis
                      dataKey="period"
                      tickFormatter={shortDay}
                      tick={{ fill: '#667085', fontSize: 11 }}
                      axisLine={false}
                      tickLine={false}
                    />
                    <YAxis tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
                    <Tooltip content={<MoneyTooltip />} />
                    <Legend />
                    <Bar dataKey="projected" name="Expected" fill="#2563eb" radius={[4, 4, 0, 0]} />
                    <Line
                      type="monotone"
                      dataKey="actual"
                      name="Tracker cash"
                      stroke="#12b76a"
                      strokeWidth={2}
                      dot={false}
                      connectNulls={false}
                    />
                  </ComposedChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-full items-center justify-center px-5">
                  <EmptyState title="No horizon" description="No daily expected cash after the last settled bank date." />
                </div>
              )}
            </div>
          </Card>
        </div>
      )}

      {view === 'insights' && (
        <div className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            {cfoCards.map((c) => (
              <Card key={c.title} title={c.title}>
                <p className="text-sm text-gray-600 dark:text-gray-300">{c.body}</p>
              </Card>
            ))}
            {!cfoCards.length && (
              <Card>
                <EmptyState
                  title="No live insight cards"
                  description="No Eligibility Sheet paid, overdue, or open audit-queue risk for the current filters."
                />
              </Card>
            )}
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <Card
              title="Documentation risk by flag"
              description="Open CPT / ICD-10 / Demographics audit risk plus Collection Denied charged — not warnings"
              padded={false}
            >
              <div className="h-72 px-2 pb-3 pt-2">
                {riskByFlag.some((r) => r.exposure_amount > 0) ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={riskByFlag} layout="vertical" barCategoryGap="18%">
                      <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" horizontal={false} />
                      <XAxis type="number" tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
                      <YAxis type="category" dataKey="label" width={140} tick={{ fill: '#667085', fontSize: 11 }} axisLine={false} tickLine={false} />
                      <Tooltip content={<MoneyTooltip />} />
                      <Bar dataKey="exposure_amount" name="Exposure" fill="#e11d48" radius={[0, 6, 6, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="flex h-full items-center justify-center px-5">
                    <EmptyState
                      title="No open audit-queue dollars"
                      description="No open CPT / ICD-10 / Demographics risk after excluding Eligibility Sheet paid status."
                    />
                  </div>
                )}
              </div>
            </Card>
            <Card
              title="CPT cash at risk"
              description={
                cptInsights && !cptInsights.insights_published
                  ? 'Headlines unpublished pending review — not shown as fact'
                  : 'Paid vs open CPT audit exposure'
              }
              padded={false}
            >
              <div className="h-72 px-2 pb-3 pt-2">
                {cptRiskBars.length ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={cptRiskBars} barCategoryGap="28%">
                      <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
                      <XAxis dataKey="label" tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
                      <YAxis tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
                      <Tooltip content={<MoneyTooltip />} />
                      <Bar dataKey="amount" name="Amount" fill="#f59e0b" radius={[6, 6, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="flex h-full items-center justify-center px-5">
                    <EmptyState
                      title={cptInsights && !cptInsights.insights_published ? 'Unpublished' : 'No CPT exposure'}
                      description={
                        cptInsights && !cptInsights.insights_published
                          ? 'CPT dollar headlines stay hidden until a super-admin publishes them.'
                          : 'No CPT at-risk dollars for the current view.'
                      }
                    />
                  </div>
                )}
              </div>
            </Card>
          </div>

          {cptInsights && (
            <div className="space-y-3">
              {!cptInsights.insights_published && (
                <Alert tone="warning">
                  CPT headline totals are unpublished pending sample review. Secondary counts are
                  visible; dollar headlines stay hidden.
                </Alert>
              )}
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <KpiCard
                  label="Identified opportunity (high+medium)"
                  value={
                    cptInsights.insights_published
                      ? money(cptInsights.headline.opportunity_high_medium)
                      : 'Unpublished'
                  }
                />
                <KpiCard
                  label="At-risk paid exposure"
                  value={
                    cptInsights.insights_published
                      ? money(cptInsights.headline.at_risk_paid)
                      : 'Unpublished'
                  }
                />
                <KpiCard
                  label="Open at-risk exposure"
                  value={
                    cptInsights.insights_published
                      ? money(cptInsights.headline.open_at_risk)
                      : 'Unpublished'
                  }
                />
                <KpiCard
                  label="Heuristic $ / missing 90901"
                  value={`${money(cptInsights.secondary.opportunity_heuristic)} · ${cptInsights.secondary.missing_90901}`}
                />
              </div>
              {hasRole('super_admin', 'sub_admin') && (
                <button
                  type="button"
                  className="text-sm text-brand-700 underline"
                  onClick={async () => {
                    await cptAuditApi.publish(!cptInsights.insights_published)
                    const next = await cptAuditApi.insights()
                    setCptInsights(next)
                  }}
                >
                  {cptInsights.insights_published ? 'Unpublish CPT headlines' : 'Publish CPT headlines'}
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {view === 'overdue' && (
        <TableCard
          title="Overdue claims"
          count={overdue.length}
          countLabel="claims"
          description="Past Insurance-behavior expected land date — not the audit queue."
        >
          <div className="mb-3 max-w-sm">
            <Input
              placeholder="Search patient, EMR, or insurance"
              value={filters.q}
              onChange={(e) => setFilters({ ...filters, q: e.target.value })}
            />
          </div>
          {overdue.length ? (
            <Table sticky>
              <THead>
                <tr>
                  <Th>Patient</Th>
                  <Th>EMR</Th>
                  <Th>DOS</Th>
                  <Th>Clinic</Th>
                  <Th>Insurance</Th>
                  <Th>CPT</Th>
                  <Th>Expected</Th>
                  <Th>Land date</Th>
                  <Th>Overdue days</Th>
                  <Th>SLA lag</Th>
                </tr>
              </THead>
              <tbody>
                {overdue.map((row, i) => (
                  <Tr key={`${row.emr_patient_id}-${row.dos}-${row.cpt_code}-${i}`}>
                    <Td>{row.patient_name || '—'}</Td>
                    <Td>
                      <EmrPatientLink id={row.emr_patient_id} facilityName={row.facility_name} />
                    </Td>
                    <Td>{row.dos || '—'}</Td>
                    <Td>{row.facility_name || '—'}</Td>
                    <Td>{row.ins_name || '—'}</Td>
                    <Td>{row.cpt_code || '—'}</Td>
                    <Td>{money(row.expected_amount)}</Td>
                    <Td>{row.expected_land_date || '—'}</Td>
                    <Td>{row.overdue_days}</Td>
                    <Td>{row.sla_lag_days == null ? '—' : row.sla_lag_days}</Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <EmptyState
              title="No overdue claims"
              description="No forecast claims past Insurance-behavior land date for the current filters."
            />
          )}
        </TableCard>
      )}
    </div>
  )
}
