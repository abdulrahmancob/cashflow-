import { useMemo, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { money, sharePct, type OverdueAnalysis } from '../../api/forecast'
import { EmptyState, Table, Td, Th, THead, Tr } from '../table'
import { Button, Card } from '../ui'
import {
  ChartFrame,
  INS_COLORS,
  KpiDelta,
  MoneyTooltip,
  OTHER_COLOR,
  pivotByName,
} from './shared'

const AGING = [
  { key: 'b0_30', name: '0–30 days', fill: '#12b76a' },
  { key: 'b31_60', name: '31–60', fill: '#f59e0b' },
  { key: 'b61_90', name: '61–90', fill: '#f97316' },
  { key: 'b90_plus', name: '90+', fill: '#e11d48' },
]

function colorFor(name: string, index: number): string {
  return name === 'Other' ? OTHER_COLOR : INS_COLORS[index % INS_COLORS.length]
}

export function OverdueKpiRow({ data }: { data: OverdueAnalysis | null | undefined }) {
  const kpi = data?.kpi
  if (!kpi) return null
  const change = kpi.overdue_change_4w
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <KpiDelta
        label="90+ days overdue"
        value={money(kpi.overdue_90_plus)}
        tone={kpi.overdue_90_plus > 0 ? 'danger' : 'ok'}
        hint="Past 90 days late. This is the book that rarely comes back in full."
      />
      <KpiDelta
        label="Expected recovery"
        value={money(kpi.expected_recovery)}
        tone="info"
        hint={`${sharePct(kpi.recovery_pct)} of ${money(kpi.face)} face value, after the age haircut.`}
      />
      <KpiDelta
        label="Overdue, 4 weeks"
        value={change == null ? '—' : money(change)}
        delta={kpi.overdue_change_4w_pct}
        goodWhen="down"
        tone={change != null && change > 0 ? 'warn' : 'ok'}
        trendLabel="vs 4 weeks"
        hint="Dollar change versus the snapshot from about 4 weeks ago. Ignores the date filter."
      />
      <KpiDelta
        label="Top 3 payers"
        value={sharePct(kpi.top3_share)}
        tone={kpi.top3_share > 60 ? 'warn' : 'default'}
        hint="Share of overdue dollars. Above 60% means one slowdown moves the month."
      />
    </div>
  )
}

export function OverdueMonthChart({ data }: { data: OverdueAnalysis | null | undefined }) {
  const [basis, setBasis] = useState<'dos' | 'land'>('dos')
  const chart = useMemo(() => {
    const rows = (data?.by_month ?? [])
      .filter((row) => row.basis === basis)
      .map((row) => ({ period: row.period, ins_name: row.ins_name, amount: row.amount }))
    return pivotByName(rows)
  }, [data, basis])
  return (
    <Card
      title="Overdue by month and insurance"
      description={
        basis === 'dos'
          ? 'Service month: which month’s visits are still unpaid. Top 6 payers, the rest as Other.'
          : 'Expected land month: the month the money should have arrived and did not. Top 6 payers, the rest as Other.'
      }
      action={
        <div className="flex gap-1">
          <Button type="button" size="sm" variant={basis === 'dos' ? 'primary' : 'secondary'} onClick={() => setBasis('dos')}>
            Service month
          </Button>
          <Button type="button" size="sm" variant={basis === 'land' ? 'primary' : 'secondary'} onClick={() => setBasis('land')}>
            Land month
          </Button>
        </div>
      }
      padded={false}
    >
      <ChartFrame
        ready={chart.points.length > 0}
        emptyTitle="No overdue by month"
        emptyDescription="No overdue dollars for the current clinic, insurance, or dates."
        height="h-80"
      >
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={chart.points}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
            <XAxis dataKey="period" tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
            <Tooltip content={<MoneyTooltip />} />
            <Legend />
            {chart.names.map((name, i) => (
              <Bar key={name} dataKey={name} name={name} stackId="od" fill={colorFor(name, i)} />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>
    </Card>
  )
}

export function OverdueAgingAndPareto({ data }: { data: OverdueAnalysis | null | undefined }) {
  const aging = data?.aging ?? []
  const pareto = data?.pareto?.rows ?? []
  const top3 = data?.pareto?.top3_share ?? 0
  const top5 = data?.pareto?.top5_share ?? 0
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="AR aging by insurance" description="How old the overdue is. Green is still recoverable. Red is 90+ days." padded={false}>
        <ChartFrame ready={aging.length > 0} emptyTitle="No aging" emptyDescription="No overdue dollars for the current filters.">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={aging} layout="vertical" barCategoryGap="18%">
              <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" horizontal={false} />
              <XAxis type="number" tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
              <YAxis type="category" dataKey="ins_name" width={120} tick={{ fill: '#667085', fontSize: 11 }} axisLine={false} tickLine={false} />
              <Tooltip content={<MoneyTooltip />} />
              <Legend />
              {AGING.map((bucket) => (
                <Bar key={bucket.key} dataKey={bucket.key} name={bucket.name} stackId="age" fill={bucket.fill} />
              ))}
            </BarChart>
          </ResponsiveContainer>
        </ChartFrame>
      </Card>
      <Card
        title="Payer concentration"
        description={`Top 3 hold ${sharePct(top3)} of overdue. Top 5 hold ${sharePct(top5)}.`}
        padded={false}
      >
        <ChartFrame ready={pareto.length > 0} emptyTitle="No concentration" emptyDescription="No overdue payers for the current filters.">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={pareto}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
              <XAxis dataKey="ins_name" tick={{ fill: '#667085', fontSize: 11 }} axisLine={false} tickLine={false} interval={0} />
              <YAxis yAxisId="left" tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
              <YAxis yAxisId="right" orientation="right" domain={[0, 100]} tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
              <Tooltip content={<MoneyTooltip />} />
              <Legend />
              <Bar yAxisId="left" dataKey="amount" name="Overdue" fill="#f59e0b" radius={[4, 4, 0, 0]} />
              <Line yAxisId="right" type="monotone" dataKey="cumulative_pct" name="Cumulative %" stroke="#2563eb" strokeWidth={2} dot={false} />
            </ComposedChart>
          </ResponsiveContainer>
        </ChartFrame>
      </Card>
    </div>
  )
}

export function DaysToPayTable({
  data,
  onOpenInsurance,
}: {
  data: OverdueAnalysis | null | undefined
  onOpenInsurance: (ins: string) => void
}) {
  const rows = data?.days_to_pay ?? []
  return (
    <Card
      title="Days to pay by insurance"
      description="Click a payer to open the overdue worklist for that book."
      padded={false}
    >
      {rows.length ? (
        <div className="max-h-96 overflow-y-auto">
          <Table>
            <THead>
              <Tr>
                <Th>Insurance</Th>
                <Th className="text-right">Overdue</Th>
                <Th className="text-right">Claims</Th>
                <Th className="text-right">Avg days</Th>
                <Th className="text-right">SLA lag</Th>
                <Th className="text-right">90+</Th>
                <Th className="text-right">Recovery</Th>
              </Tr>
            </THead>
            <tbody>
              {rows.map((row) => (
                <Tr key={row.ins_name} onClick={() => onOpenInsurance(row.ins_name)} className="cursor-pointer">
                  <Td>{row.ins_name}</Td>
                  <Td className="text-right">{money(row.amount)}</Td>
                  <Td className="text-right">{row.claims.toLocaleString()}</Td>
                  <Td className="text-right">{row.avg_overdue_days.toFixed(1)}</Td>
                  <Td className="text-right">{row.avg_sla_lag_days == null ? '—' : row.avg_sla_lag_days.toFixed(1)}</Td>
                  <Td className="text-right">{sharePct(row.share_90_plus)}</Td>
                  <Td className="text-right">{money(row.expected_recovery)}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      ) : (
        <div className="px-5 py-8">
          <EmptyState title="No payer timing" description="No overdue payers for the current filters." />
        </div>
      )}
    </Card>
  )
}

export function OverdueByClinic({ data }: { data: OverdueAnalysis | null | undefined }) {
  const rows = data?.by_clinic ?? []
  return (
    <Card title="Overdue by clinic" description="Which clinic’s AR is sitting past the expected land date." padded={false}>
      <ChartFrame ready={rows.length > 0} emptyTitle="No clinic overdue" emptyDescription="No overdue dollars for the current filters.">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} layout="vertical" barCategoryGap="18%">
            <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" horizontal={false} />
            <XAxis type="number" tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
            <YAxis type="category" dataKey="facility_name" width={120} tick={{ fill: '#667085', fontSize: 11 }} axisLine={false} tickLine={false} />
            <Tooltip content={<MoneyTooltip />} />
            <Bar dataKey="amount" name="Overdue" fill="#f59e0b" radius={[0, 6, 6, 0]}>
              {rows.map((row) => (
                <Cell key={row.facility_name} fill="#f59e0b" />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>
    </Card>
  )
}

export function ChaseList({
  data,
  onOpenClaim,
}: {
  data: OverdueAnalysis | null | undefined
  onOpenClaim: (row: { ins_name: string; emr_patient_id: string }) => void
}) {
  const rows = data?.chase_list ?? []
  return (
    <Card
      title="Chase list"
      description="Top 15 overdue claims by expected recovery. A call here returns more than a call on older, smaller dollars."
      padded={false}
    >
      {rows.length ? (
        <div className="max-h-96 overflow-y-auto">
          <Table>
            <THead>
              <Tr>
                <Th>Patient</Th>
                <Th>Insurance</Th>
                <Th>DOS</Th>
                <Th className="text-right">Expected</Th>
                <Th className="text-right">Days</Th>
                <Th className="text-right">Recovery</Th>
              </Tr>
            </THead>
            <tbody>
              {rows.map((row, i) => (
                <Tr
                  key={`${row.emr_patient_id}-${row.dos}-${i}`}
                  onClick={() => onOpenClaim(row)}
                  className="cursor-pointer"
                >
                  <Td>{row.patient_name || row.emr_patient_id || '—'}</Td>
                  <Td>{row.ins_name || '—'}</Td>
                  <Td>{row.dos || '—'}</Td>
                  <Td className="text-right">{money(row.expected_amount)}</Td>
                  <Td className="text-right">{row.overdue_days}</Td>
                  <Td className="text-right">{money(row.recovery_amount)}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      ) : (
        <div className="px-5 py-8">
          <EmptyState title="Nothing to chase" description="No overdue claims past the expected land date for the current filters." />
        </div>
      )}
    </Card>
  )
}
