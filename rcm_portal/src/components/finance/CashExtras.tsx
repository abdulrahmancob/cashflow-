import { useMemo } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { money, sharePct, type CashOverview } from '../../api/forecast'
import { Card } from '../ui'
import { ChartFrame, INS_COLORS, KpiDelta, MoneyTooltip, OTHER_COLOR, pivotByName, shortDay } from './shared'

export function CashKpiStrip({ kpi }: { kpi: CashOverview['kpi'] | null | undefined }) {
  if (!kpi) return null
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      <KpiDelta
        label="Cash month to date"
        value={money(kpi.cash_mtd)}
        tone="ok"
        hint={kpi.through ? `Tracker through ${kpi.through}. Not split by clinic.` : 'Tracker cash. Not split by clinic.'}
      />
      <KpiDelta
        label="Forecast month to date"
        value={money(kpi.forecast_mtd)}
        tone="info"
        hint="Closest prior forecast for the same days."
      />
      <KpiDelta
        label="Pace"
        value={kpi.pace_pct == null ? '—' : sharePct(kpi.pace_pct)}
        tone={kpi.pace_pct != null && kpi.pace_pct < 95 ? 'warn' : 'ok'}
        hint="Actual divided by forecast, month to date."
      />
      <KpiDelta
        label="Next 10 days"
        value={money(kpi.next_10d)}
        tone="info"
        hint="Forecast after the last settled bank day."
      />
      <KpiDelta
        label="Next 30 days"
        value={money(kpi.next_30d)}
        tone="info"
        hint="Forecast stitch when it covers the window, otherwise open AR landing in 30 days."
      />
      <KpiDelta
        label="Forecast error, 30 days"
        value={kpi.mae_30d == null ? '—' : sharePct(kpi.mae_30d)}
        tone={kpi.mae_30d != null && kpi.mae_30d > 10 ? 'warn' : 'default'}
        hint="Mean absolute day-ahead error. Lower is a forecast you can plan on."
      />
    </div>
  )
}

export function MonthBurnup({ rows }: { rows: CashOverview['burnup'] | null | undefined }) {
  const data = rows ?? []
  return (
    <Card
      title="This month, cumulative"
      description="Tracker cash against the forecast. The dotted line is the rest of the month. Clinic and insurance filters do not split tracker cash."
      padded={false}
    >
      <ChartFrame ready={data.length > 0} emptyTitle="No month yet" emptyDescription="No tracker cash or forecast for the current month." height="h-80">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
            <XAxis dataKey="period" tickFormatter={shortDay} tick={{ fill: '#667085', fontSize: 11 }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
            <Tooltip content={<MoneyTooltip />} />
            <Legend />
            <Line type="monotone" dataKey="actual_cum" name="Tracker cash" stroke="#12b76a" strokeWidth={2} dot={false} connectNulls={false} />
            <Line type="monotone" dataKey="forecast_to_date" name="Forecast to date" stroke="#2563eb" strokeWidth={2} dot={false} connectNulls={false} />
            <Line type="monotone" dataKey="forecast_rest" name="Forecast rest of month" stroke="#2563eb" strokeWidth={2} strokeDasharray="5 4" dot={false} connectNulls={false} />
          </LineChart>
        </ResponsiveContainer>
      </ChartFrame>
    </Card>
  )
}

export function ForecastAccuracy({ accuracy }: { accuracy: CashOverview['accuracy'] | null | undefined }) {
  const days = accuracy?.days ?? []
  const recent = accuracy?.recent ?? []
  return (
    <Card
      title="Forecast accuracy"
      description={
        accuracy?.yesterday
          ? `Latest bank day ${accuracy.yesterday.bank_date}: forecast ${money(accuracy.yesterday.forecast_total)}, tracker ${money(accuracy.yesterday.actual_total)}.`
          : 'Day-ahead error versus the frozen tracker.'
      }
      padded={false}
    >
      {recent.length > 0 && (
        <p className="px-5 pt-3 text-xs text-gray-500">
          Last bank days:{' '}
          {recent
            .map((row) => `${row.bank_date.slice(5)} ${row.error_pct == null ? '—' : `${row.error_pct}%`}`)
            .join(' · ')}
        </p>
      )}
      <ChartFrame ready={days.length > 0} emptyTitle="No accuracy history" emptyDescription="No day-ahead scores yet.">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={days}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
            <XAxis dataKey="bank_date" tickFormatter={shortDay} tick={{ fill: '#667085', fontSize: 11 }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
            <Tooltip content={<MoneyTooltip />} />
            <Bar dataKey="error_pct" name="Error %" fill="#6366f1" radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>
    </Card>
  )
}

export function ForwardWeeks({ rows }: { rows: CashOverview['forward_weeks'] | null | undefined }) {
  const data = rows ?? []
  return (
    <Card
      title="Next 30 days by week"
      description="On track is expected to land that week. Overdue recovery is late dollars whose land date still falls in the week, weighted by age. Hatched amber is hoped-for, not counted cash."
      padded={false}
    >
      <ChartFrame ready={data.length > 0} emptyTitle="No forward AR" emptyDescription="Nothing is expected to land in the next 30 days.">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data}>
            <defs>
              <pattern id="overdueHatch" patternUnits="userSpaceOnUse" width="6" height="6" patternTransform="rotate(45)">
                <rect width="6" height="6" fill="#f59e0b" />
                <line x1="0" y1="0" x2="0" y2="6" stroke="#fff7ed" strokeWidth="2" />
              </pattern>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
            <XAxis dataKey="week" tickFormatter={shortDay} tick={{ fill: '#667085', fontSize: 11 }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
            <Tooltip content={<MoneyTooltip />} />
            <Legend />
            <Bar dataKey="on_track" name="On track" stackId="fwd" fill="#2563eb" />
            <Bar dataKey="overdue_recovery" name="Overdue recovery" stackId="fwd" fill="url(#overdueHatch)" />
          </ComposedChart>
        </ResponsiveContainer>
      </ChartFrame>
    </Card>
  )
}

export function PayerMonths({ rows }: { rows: CashOverview['payer_months'] | null | undefined }) {
  const chart = useMemo(
    () => pivotByName((rows ?? []).map((row) => ({ period: row.period, ins_name: row.ins_name, amount: row.paid_amount }))),
    [rows],
  )
  return (
    <Card
      title="Cash by payer by month"
      description="Tracker deposits. Top payers in the window. Not split by clinic."
      padded={false}
    >
      <ChartFrame ready={chart.points.length > 0} emptyTitle="No payer cash" emptyDescription="No tracker deposits for the current insurance or dates." height="h-80">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={chart.points}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
            <XAxis dataKey="period" tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
            <Tooltip content={<MoneyTooltip />} />
            <Legend />
            {chart.names.map((name, i) => (
              <Bar
                key={name}
                dataKey={name}
                name={name}
                stackId="payer"
                fill={name === 'Other' ? OTHER_COLOR : INS_COLORS[i % INS_COLORS.length]}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>
    </Card>
  )
}
