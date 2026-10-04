import { useEffect, useMemo, useRef, useState } from 'react'
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
import { forecastApi, money, monthLastDay, type CashOverview, type Filters } from '../../api/forecast'
import { EmptyState } from '../../components/table'
import { Card } from '../../components/ui'
import { CashKpiStrip, ForecastAccuracy, ForwardWeeks, MonthBurnup, PayerMonths } from '../../components/finance/CashExtras'
import { isoAddDays, MoneyTooltip, shortDay } from '../../components/finance/shared'

type DailyPoint = {
  period: string
  amount: number
  horizon_kind?: string
  forecast_as_of?: string | null
}

export function CashTrajectory({
  filters,
  onError,
}: {
  filters: Filters
  onError: (message: string) => void
}) {
  const [bundle, setBundle] = useState<CashOverview | null>(null)
  const sig = JSON.stringify(filters)
  const prev = useRef('')

  useEffect(() => {
    const changed = prev.current !== '' && prev.current !== sig
    prev.current = sig
    let cancelled = false
    const handle = window.setTimeout(() => {
      ;(async () => {
        try {
          onError('')
          const next = await forecastApi.cashOverview(filters)
          if (!cancelled) setBundle(next)
        } catch (e) {
          if (!cancelled) onError(String((e as Error).message || e))
        }
      })()
    }, changed ? 250 : 0)
    return () => {
      cancelled = true
      window.clearTimeout(handle)
    }
  }, [filters, onError, sig])

  const daily: DailyPoint[] = bundle?.daily ?? []
  const actual: DailyPoint[] = bundle?.actual ?? []
  const monthly = bundle?.monthly ?? []
  const settled = bundle?.last_settled_date || actual.reduce((m, r) => (r.period > m ? r.period : m), '')
  const splitFilters = filters.facility.length > 0 || filters.ins.length > 0
  const bankHint = 'Tracker cash is company-wide — not split by clinic or insurance'

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
    const map = new Map<string, { period: string; projected: number; actual: number | null; forecast_as_of?: string | null }>()
    for (const r of daily) {
      map.set(r.period, { period: r.period, projected: r.amount, actual: null, forecast_as_of: r.forecast_as_of })
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
    const rows: Array<{ period: string; projected: number; actual: number | null; horizon_kind?: string }> = []
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

  if (!bundle) {
    return (
      <div className="flex min-h-[24rem] items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-gray-200 border-t-brand-600" />
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <CashKpiStrip kpi={bundle.kpi} />
      <MonthBurnup rows={bundle.burnup} />
      {splitFilters && (
        <p className="text-xs text-gray-400">{bankHint}. Monthly landed vs expected is hidden while clinic or insurance is selected.</p>
      )}
      {!splitFilters && (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card title="Landed vs expected" description="Tracker cash vs closest prior forecast. Settled months only." padded={false}>
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
          <Card title="Variance by month" description="Tracker cash minus closest prior forecast. Green beat, red miss. Incomplete months omitted." padded={false}>
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
      <ForecastAccuracy accuracy={bundle.accuracy} />
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
                <XAxis dataKey="period" tickFormatter={shortDay} tick={{ fill: '#667085', fontSize: 11 }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
                <Tooltip content={<MoneyTooltip />} />
                <Legend />
                <Bar dataKey="projected" name="Expected" fill="#2563eb" radius={[4, 4, 0, 0]} />
                <Line type="monotone" dataKey="actual" name="Tracker cash" stroke="#12b76a" strokeWidth={2} dot={false} connectNulls={false} />
              </ComposedChart>
            </ResponsiveContainer>
          ) : (
            <div className="flex h-full items-center justify-center px-5">
              <EmptyState title="No horizon" description="No daily expected cash after the last settled bank date." />
            </div>
          )}
        </div>
      </Card>
      <ForwardWeeks rows={bundle.forward_weeks} />
      <PayerMonths rows={bundle.payer_months} />
    </div>
  )
}
