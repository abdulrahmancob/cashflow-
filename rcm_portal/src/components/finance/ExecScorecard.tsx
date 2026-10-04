import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { money, sharePct, type ExecScorecard } from '../../api/forecast'
import { EmptyState, Table, Td, Th, THead, Tr } from '../table'
import { Card } from '../ui'
import { ChartFrame, KpiDelta, MoneyTooltip } from './shared'

const GRADE_TONE: Record<string, string> = {
  A: 'text-emerald-700 dark:text-emerald-400',
  B: 'text-sky-700 dark:text-sky-400',
  C: 'text-amber-700 dark:text-amber-400',
  D: 'text-rose-700 dark:text-rose-400',
}

function tileValue(tile: ExecScorecard['tiles'][number]): string {
  if (tile.value == null) return '—'
  if (tile.unit === 'money') return money(tile.value)
  if (tile.unit === 'days') return `${Number(tile.value).toFixed(1)} days`
  return sharePct(tile.value)
}

export function ScorecardTiles({ data }: { data: ExecScorecard | null | undefined }) {
  const tiles = data?.tiles ?? []
  if (!tiles.length) return null
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      {tiles.map((tile) => (
        <KpiDelta
          key={tile.key}
          label={tile.label}
          value={tileValue(tile)}
          delta={tile.delta_pct}
          goodWhen={tile.good_when}
          tone={tile.good_when === 'down' && (tile.delta_pct ?? 0) > 0 ? 'warn' : 'default'}
          hint={tile.hint}
        />
      ))}
    </div>
  )
}

export function ArTrendChart({ data }: { data: ExecScorecard | null | undefined }) {
  const rows = data?.trend ?? []
  const narrative = data?.narrative
  const direction =
    narrative?.ar_change_4w == null
      ? 'Weekly snapshots. The date filter does not apply.'
      : narrative.ar_change_4w > 0
        ? `Overdue is up ${money(narrative.ar_change_4w)} versus 4 weeks ago. The date filter does not apply.`
        : narrative.ar_change_4w < 0
          ? `Overdue is down ${money(Math.abs(narrative.ar_change_4w))} versus 4 weeks ago. The date filter does not apply.`
          : 'Overdue is flat versus 4 weeks ago. The date filter does not apply.'
  return (
    <Card title="AR trend" description={direction} padded={false}>
      <ChartFrame ready={rows.length > 0} emptyTitle="No snapshots" emptyDescription="Need more than one successful forecast week.">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={rows}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
            <XAxis dataKey="as_of" tick={{ fill: '#667085', fontSize: 11 }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
            <Tooltip content={<MoneyTooltip />} />
            <Legend />
            <Line type="monotone" dataKey="overdue" name="Overdue" stroke="#f59e0b" strokeWidth={2} dot={false} />
            <Line type="monotone" dataKey="on_track" name="On track" stroke="#2563eb" strokeWidth={2} dot={false} />
            <Line type="monotone" dataKey="overdue_90_plus" name="90+ overdue" stroke="#e11d48" strokeWidth={2} dot={false} />
          </LineChart>
        </ResponsiveContainer>
      </ChartFrame>
    </Card>
  )
}

export function CollectionRateChart({ data }: { data: ExecScorecard | null | undefined }) {
  const rows = data?.collection_rate ?? []
  return (
    <Card
      title="Collection rate by service month"
      description="Paid divided by expected for that month of service. Lighter bars are still maturing, so a low rate there is not a miss."
      padded={false}
    >
      <ChartFrame ready={rows.length > 0} emptyTitle="No collection rate" emptyDescription="No service-month expected dollars for the current filters.">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
            <XAxis dataKey="period" tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
            <YAxis domain={[0, 100]} tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
            <Tooltip content={<MoneyTooltip />} />
            <Bar dataKey="rate_pct" name="Collection %" radius={[4, 4, 0, 0]}>
              {rows.map((row) => (
                <Cell key={row.period} fill={row.maturing ? '#cbd5e1' : '#2563eb'} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>
    </Card>
  )
}

export function LeakageWaterfall({ data }: { data: ExecScorecard | null | undefined }) {
  const steps = data?.leakage ?? []
  let cursor = 0
  const chart = steps.map((step) => {
    if (step.kind === 'total' || step.kind === 'result') {
      const row = { label: step.label, base: 0, value: Math.max(step.amount, 0), kind: step.kind }
      cursor = step.amount
      return row
    }
    const next = cursor - step.amount
    const row = { label: step.label, base: Math.max(next, 0), value: step.amount, kind: step.kind }
    cursor = next
    return row
  })
  return (
    <Card
      title="Revenue leakage"
      description="Where expected dollars go: denied, rejected, zero pay, and open documentation risk. Collectible is what is left."
      padded={false}
    >
      <ChartFrame ready={steps.some((step) => step.amount !== 0)} emptyTitle="No leakage view" emptyDescription="No outcome stages for the current filters.">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={chart}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" vertical={false} />
            <XAxis dataKey="label" tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
            <YAxis tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
            <Tooltip content={<MoneyTooltip />} />
            <Bar dataKey="base" stackId="wf" fill="transparent" />
            <Bar dataKey="value" name="Amount" stackId="wf" radius={[4, 4, 0, 0]}>
              {chart.map((row) => (
                <Cell
                  key={row.label}
                  fill={row.kind === 'loss' ? '#e11d48' : row.kind === 'result' ? '#12b76a' : '#2563eb'}
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>
    </Card>
  )
}

export function PayerScorecardTable({ data }: { data: ExecScorecard | null | undefined }) {
  const rows = data?.payer_scorecard ?? []
  return (
    <Card
      title="Payer scorecard"
      description="A and B pay on time and in full. D is a contract that costs more to collect than it returns. Cash is tracker deposits over 90 days."
      padded={false}
    >
      {rows.length ? (
        <div className="max-h-[28rem] overflow-y-auto">
          <Table>
            <THead>
              <Tr>
                <Th>Grade</Th>
                <Th>Insurance</Th>
                <Th className="text-right">Cash 90d</Th>
                <Th className="text-right">Share</Th>
                <Th className="text-right">Collection</Th>
                <Th className="text-right">Avg days</Th>
                <Th className="text-right">Overdue</Th>
                <Th className="text-right">90+</Th>
                <Th className="text-right">Denied</Th>
              </Tr>
            </THead>
            <tbody>
              {rows.map((row) => (
                <Tr key={row.ins_name}>
                  <Td className={`font-semibold ${GRADE_TONE[row.grade] || ''}`}>{row.grade || '—'}</Td>
                  <Td>{row.ins_name}</Td>
                  <Td className="text-right">{money(row.cash_90d)}</Td>
                  <Td className="text-right">{sharePct(row.revenue_share)}</Td>
                  <Td className="text-right">{row.collection_rate == null ? '—' : sharePct(row.collection_rate)}</Td>
                  <Td className="text-right">{row.avg_days == null ? '—' : row.avg_days.toFixed(1)}</Td>
                  <Td className="text-right">{money(row.overdue)}</Td>
                  <Td className="text-right">{sharePct(row.share_90_plus)}</Td>
                  <Td className="text-right">{money(row.denied)}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </div>
      ) : (
        <div className="px-5 py-8">
          <EmptyState title="No payer scorecard" description="No tracker cash or open AR for the current filters." />
        </div>
      )}
    </Card>
  )
}

export function RecoveryBars({ data }: { data: ExecScorecard | null | undefined }) {
  const rows = data?.recovery ?? []
  return (
    <Card
      title="Face value vs expected recovery"
      description="Face is what the forecast still expects. Recovery is that amount times the chance it still arrives, which falls as the claim ages."
      padded={false}
    >
      <ChartFrame ready={rows.length > 0} emptyTitle="No recovery view" emptyDescription="No overdue dollars for the current filters.">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} layout="vertical" barCategoryGap="18%">
            <CartesianGrid strokeDasharray="3 3" stroke="#e4e7ec" horizontal={false} />
            <XAxis type="number" tick={{ fill: '#667085', fontSize: 12 }} axisLine={false} tickLine={false} />
            <YAxis type="category" dataKey="ins_name" width={120} tick={{ fill: '#667085', fontSize: 11 }} axisLine={false} tickLine={false} />
            <Tooltip content={<MoneyTooltip />} />
            <Legend />
            <Bar dataKey="face" name="Face value" fill="#f59e0b" radius={[0, 4, 4, 0]} />
            <Bar dataKey="expected_recovery" name="Expected recovery" fill="#12b76a" radius={[0, 4, 4, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </ChartFrame>
    </Card>
  )
}
