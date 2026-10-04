import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { forecastApi, money, pct, type Filters, type OverdueAnalysis } from '../../api/forecast'
import {
  ChaseList,
  DaysToPayTable,
  OverdueAgingAndPareto,
  OverdueByClinic,
  OverdueKpiRow,
  OverdueMonthChart,
} from '../../components/finance/OverdueAnalytics'
import { INS_COLORS, InsuranceMixList, MoneyTooltip, PaidTooltip } from '../../components/finance/shared'
import { EmptyState, Table, Td, Th, THead, Tr } from '../../components/table'
import { Button, Card, KpiCard } from '../../components/ui'

type OutcomesSummary = Awaited<ReturnType<typeof forecastApi.outcomesSummary>>
type BehaviorTrend = Awaited<ReturnType<typeof forecastApi.behaviorTrend>>

export function MissionControl({
  filters,
  onError,
  onOpenInsurance,
  onOpenClaim,
}: {
  filters: Filters
  onError: (message: string) => void
  onOpenInsurance: (ins: string) => void
  onOpenClaim: (row: { ins_name: string; emr_patient_id: string }) => void
}) {
  const [behaviorView, setBehaviorView] = useState<'chart' | 'checks'>('chart')
  const [kpi, setKpi] = useState<Record<string, number | string | boolean>>({})
  const [summary, setSummary] = useState<OutcomesSummary | null>(null)
  const [behavior, setBehavior] = useState<BehaviorTrend | null>(null)
  const [analysis, setAnalysis] = useState<OverdueAnalysis | null>(null)
  const [missionLoading, setMissionLoading] = useState(true)
  const [dayAhead, setDayAhead] = useState<{
    yesterday: { bank_date: string; forecast_total: number; actual_total: number; error_pct: number | null } | null
    recent: Array<{ bank_date: string; error_pct: number | null }>
  } | null>(null)
  const [checkRows, setCheckRows] = useState<NonNullable<BehaviorTrend>['checks']>([])
  const [checksLoading, setChecksLoading] = useState(false)
  const filterSig = JSON.stringify(filters)
  const prevFilterSig = useRef(filterSig)
  const missionPainted = useRef(false)
  const checksLoadedFor = useRef('')

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
          onError('')
          if (!missionPainted.current) setMissionLoading(true)
          const bundle = await forecastApi.mission(filters)
          if (!cancelled) {
            setKpi(bundle.kpi)
            setSummary(bundle.outcomes)
            setBehavior(bundle.behavior)
            setDayAhead(bundle.day_ahead || null)
            setAnalysis(bundle.overdue_analysis ?? null)
            missionPainted.current = true
            setMissionLoading(false)
          }
        } catch (e) {
          if (!cancelled) {
            onError(String((e as Error).message || e))
            setMissionLoading(false)
          }
        }
      })()
    }, delay)
    return () => {
      cancelled = true
      window.clearTimeout(handle)
    }
  }, [filters, filterSig, onError])

  useEffect(() => {
    if (behaviorView !== 'checks') return
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
  }, [behaviorView, filters, filterSig])

  const overdueRows = useMemo(
    () => [...(summary?.overdue_by_insurance ?? [])].sort((a, b) => Number(b.expected_payment) - Number(a.expected_payment)),
    [summary],
  )
  const riskRows = useMemo(
    () => [...(summary?.risk_by_insurance ?? [])].sort((a, b) => Number(b.exposure_amount) - Number(a.exposure_amount)),
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
    () => (summary?.stages ?? []).map((s) => ({ ...s, label: String(s.outcome_stage || '').replace(/_/g, ' ') })),
    [summary],
  )
  const behaviorChart = useMemo(() => {
    const series = behavior?.series ?? []
    const names = behavior?.insurers?.length ? behavior.insurers : [...new Set(series.map((r) => r.ins_name))]
    const top = names.slice(0, 2)
    const byDay = new Map<string, Record<string, string | number | null>>()
    for (const r of series) {
      if (!top.includes(r.ins_name)) continue
      const day = r.period
      if (!day) continue
      const row = byDay.get(day) || { period: day }
      row[r.ins_name] = Number(row[r.ins_name] || 0) + Number(r.paid_amount || 0)
      row[`${r.ins_name}__n`] = Number(row[`${r.ins_name}__n`] || 0) + Number(r.check_count || 0)
      byDay.set(day, row)
    }
    const points = [...byDay.values()].sort((a, b) => String(a.period).localeCompare(String(b.period)))
    return { top, points }
  }, [behavior])

  const arOpen = Number(kpi.on_track_amount ?? 0) + Number(kpi.overdue_amount ?? 0)
  const riskAmt = Number(kpi.risk_exposure_amount ?? 0)

  if (missionLoading) {
    return (
      <div className="flex min-h-[24rem] items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-gray-200 border-t-brand-600" />
      </div>
    )
  }

  return (
    <>
      {dayAhead?.yesterday && (
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
            value={dayAhead.recent.map((row) => (row.error_pct == null ? '—' : `${row.error_pct}%`)).join(' · ') || '—'}
            hint="Day-ahead error vs the frozen tracker"
          />
        </div>
      )}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <KpiCard
          label={filters.dateFrom || filters.dateTo || filters.month.length ? 'Past projected' : 'Projected AR'}
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
          label={filters.dateFrom || filters.dateTo || filters.month.length ? 'Actual in range' : 'Actual received'}
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
        <KpiCard label="On track" value={money(kpi.on_track_amount)} hint={`${pct(kpi.on_track_amount, arOpen)} of open AR`} />
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
          On track = visits in this range still inside SLA. Overdue = expected to land in this range and still late.
        </p>
      )}
      <OverdueKpiRow data={analysis} />

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
            <Button type="button" size="sm" variant={behaviorView === 'chart' ? 'primary' : 'secondary'} onClick={() => setBehaviorView('chart')}>
              Chart
            </Button>
            <Button type="button" size="sm" variant={behaviorView === 'checks' ? 'primary' : 'secondary'} onClick={() => setBehaviorView('checks')}>
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
              <EmptyState title="No tracker checks" description="No Transaction Tracker deposits for the current insurance or dates." />
            </div>
          )
        ) : (
          <div className="h-72 px-2 pb-3 pt-2">
            {behaviorChart.points.length ? (
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={behaviorChart.points}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
                  <XAxis dataKey="period" tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} tickFormatter={(v) => (String(v).length >= 10 ? String(v).slice(5) : String(v))} />
                  <YAxis tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
                  <Tooltip content={<PaidTooltip />} />
                  <Legend />
                  {behaviorChart.top.map((name, i) => (
                    <Line key={name} type="monotone" dataKey={name} name={name} stroke={INS_COLORS[i % INS_COLORS.length]} strokeWidth={2} dot={{ r: 3 }} connectNulls />
                  ))}
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex h-full items-center justify-center px-5">
                <EmptyState title="No tracker cash" description="No Transaction Tracker deposits for the current insurance or dates." />
              </div>
            )}
          </div>
        )}
      </Card>

      <OverdueMonthChart data={analysis} />

      <Card title="Insurance in / overdue / risk" description="In = Eligibility Sheet paid. Overdue = forecast land lag. Risk = open audit queue minus Eligibility Sheet paid status." padded={false}>
        {mixRows.length ? (
          <InsuranceMixList rows={mixRows} />
        ) : (
          <div className="flex h-64 items-center justify-center px-5">
            <EmptyState title="No insurance mix" description="No Eligibility Sheet paid, overdue, or open audit-queue risk for the current filters." />
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
        <Card title="At-risk by insurance" description="Open CPT / ICD-10 / Demographics audit risk plus Collection Denied charged — not warnings, not payment lag" padded={false}>
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
                <EmptyState title="No open audit-queue risk" description="No open CPT / ICD-10 / Demographics risk items for the current filters." />
              </div>
            )}
          </div>
        </Card>
      </div>

      <OverdueAgingAndPareto data={analysis} />
      <DaysToPayTable data={analysis} onOpenInsurance={onOpenInsurance} />

      <div className="grid gap-4 lg:grid-cols-2">
        <OverdueByClinic data={analysis} />
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
      </div>
      <ChaseList data={analysis} onOpenClaim={onOpenClaim} />
    </>
  )
}
