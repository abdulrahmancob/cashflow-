import type { ReactNode } from 'react'
import { forecastApi, money, sharePct } from '../../api/forecast'
import { KpiCard } from '../ui'

export const INS_COLORS = [
  '#2563eb',
  '#0ea5e9',
  '#12b76a',
  '#f59e0b',
  '#ef4444',
  '#8b5cf6',
  '#ec4899',
  '#14b8a6',
]

export const OTHER_COLOR = '#98a2b3'

export const AUDIT_DOMAIN_LABEL: Record<string, string> = {
  cpt: 'CPT',
  icd: 'ICD-10',
  demo: 'Demographics',
  denied: 'Denied',
}

type Outcomes = Awaited<ReturnType<typeof forecastApi.outcomesSummary>> | null

export type InsightCard = { title: string; body: string }

export function isoAddDays(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00`)
  if (Number.isNaN(d.getTime())) return iso
  d.setDate(d.getDate() + days)
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

export function shortDay(period: string): string {
  const d = period.slice(5)
  return d || period
}

type TipRow = {
  name?: string
  value?: number | string
  color?: string
  dataKey?: string
  payload?: Record<string, unknown>
}

export function PaidTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean
  payload?: TipRow[]
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

export function MoneyTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean
  payload?: TipRow[]
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
        const key = String(p.dataKey || '')
        const name = String(p.name || '')
        const isDays = name.toLowerCase().includes('lag') || key.includes('days')
        const isPct = key.endsWith('_pct') || key.includes('share') || name.includes('%')
        const shown = isDays
          ? `${n.toFixed(1)} days`
          : isPct
            ? sharePct(n)
            : money(n)
        return (
          <div key={p.dataKey || p.name || i} className="flex items-center gap-2 text-sm">
            <span className="h-2 w-2 rounded-full" style={{ background: p.color }} />
            <span className="text-gray-500">{p.name || p.dataKey}</span>
            <span className="font-semibold text-gray-900 dark:text-white">{shown}</span>
            {share != null && !isDays && !isPct && (
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

export function InsuranceMixList({
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

  const bar = (row: { landed: number; overdue: number; risk: number }) => (
    <div className="flex h-2.5 min-w-0 flex-1 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
      {MIX_SEGMENTS.map((s) => {
        const amt = row[s.key]
        if (amt <= 0) return null
        return (
          <div
            key={s.key}
            title={`${s.label}: ${money(amt)}`}
            className="h-full"
            style={{ background: s.bar, flexGrow: amt, minWidth: 3 }}
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
        {rows.map((row) => (
          <div key={row.ins_name} className="flex items-center gap-3">
            <div
              className="w-36 shrink-0 truncate text-xs font-medium text-gray-700 dark:text-gray-300"
              title={row.ins_name}
            >
              {row.ins_name}
            </div>
            {bar(row)}
            {amountCols(row)}
          </div>
        ))}
      </div>
      <div className="mt-3 flex items-center gap-3 border-t border-gray-200 pt-3 dark:border-gray-700">
        <div className="w-36 shrink-0 text-xs font-semibold text-gray-900 dark:text-white">Total</div>
        {bar(totals)}
        {amountCols(totals, true)}
      </div>
      {grand <= 0 ? null : null}
    </div>
  )
}

export function buildCfoInsightCards(
  summary: Outcomes | null,
  kpi: Record<string, number | string | boolean>,
): InsightCard[] {
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

  const cards: InsightCard[] = []
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

export type ExecNarrative = {
  ar_change_4w: number | null
  ar_change_4w_pct: number | null
  collection_rate: number | null
  collection_prior: number | null
  collection_period: string
  top3_share: number
  recovery_haircut: number
  recovery_face: number
  expected_recovery: number
  worst_payer: string
  worst_payer_90: number
}

export function buildExecStoryCards(
  summary: Outcomes | null,
  kpi: Record<string, number | string | boolean>,
  narrative: ExecNarrative | null | undefined,
): InsightCard[] {
  const cards = buildCfoInsightCards(summary, kpi)
  if (!narrative) return cards
  if (narrative.ar_change_4w != null) {
    const delta = narrative.ar_change_4w
    const direction = delta > 0 ? 'higher' : delta < 0 ? 'lower' : 'flat'
    cards.push({
      title: delta > 0 ? 'AR is getting worse' : delta < 0 ? 'AR is improving' : 'AR is flat',
      body: `Overdue is ${money(Math.abs(delta))} ${direction} than 4 weeks ago${
        narrative.ar_change_4w_pct == null ? '' : ` (${narrative.ar_change_4w_pct}%)`
      }. ${delta > 0 ? 'Work the 90+ bucket before more of it ages out.' : 'Keep the same chase order so the gain holds.'}`,
    })
  }
  if (narrative.collection_rate != null && narrative.collection_prior != null) {
    const moved = narrative.collection_rate - narrative.collection_prior
    cards.push({
      title: 'Collection rate',
      body: `Service month ${narrative.collection_period || 'latest'} collected ${narrative.collection_rate.toFixed(1)}% versus ${narrative.collection_prior.toFixed(1)}% before (${moved >= 0 ? '+' : ''}${moved.toFixed(1)} pts). Follow the gap before it becomes overdue.`,
    })
  }
  if (narrative.top3_share > 60) {
    cards.push({
      title: 'Payer concentration',
      body: `The top 3 payers hold ${narrative.top3_share.toFixed(1)}% of overdue. A slowdown there moves the whole month.`,
    })
  }
  if (narrative.recovery_face > 0) {
    cards.push({
      title: 'Aging haircut',
      body: `Face overdue is ${money(narrative.recovery_face)}. Expected recovery is ${money(narrative.expected_recovery)}, so ${money(narrative.recovery_haircut)} is the aging haircut. Chase the newest overdue first.`,
    })
  }
  if (narrative.worst_payer && narrative.worst_payer_90 > 0) {
    cards.push({
      title: 'Oldest book',
      body: `${narrative.worst_payer} holds the most 90+ overdue at ${money(narrative.worst_payer_90)}. Call that book first.`,
    })
  }
  return cards
}

export function KpiDelta({
  label,
  value,
  tone = 'default',
  delta,
  goodWhen = 'up',
  hint,
  trendLabel = 'vs prior',
}: {
  label: string
  value: string
  tone?: 'default' | 'warn' | 'ok' | 'danger' | 'info'
  delta?: number | null
  goodWhen?: 'up' | 'down'
  hint?: string
  trendLabel?: string
}) {
  return (
    <KpiCard
      label={label}
      value={value}
      tone={tone}
      trend={delta == null || !Number.isFinite(delta) ? undefined : delta}
      invertTrend={goodWhen === 'down'}
      trendLabel={trendLabel}
      hint={hint}
    />
  )
}

export function ChartFrame({
  ready,
  emptyTitle,
  emptyDescription,
  height = 'h-72',
  children,
}: {
  ready: boolean
  emptyTitle: string
  emptyDescription: string
  height?: string
  children: ReactNode
}) {
  if (!ready) {
    return (
      <div className={`flex ${height} items-center justify-center px-5`}>
        <div className="text-center">
          <div className="text-sm font-medium text-gray-900 dark:text-white">{emptyTitle}</div>
          <p className="mt-1 text-sm text-gray-500">{emptyDescription}</p>
        </div>
      </div>
    )
  }
  return <div className={`${height} px-2 pb-3 pt-2`}>{children}</div>
}

export function pivotByName(
  rows: Array<{ period: string; ins_name: string; amount: number }>,
): { names: string[]; points: Array<Record<string, string | number>> } {
  const totals = new Map<string, number>()
  for (const row of rows) totals.set(row.ins_name, (totals.get(row.ins_name) || 0) + row.amount)
  const names = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([name]) => name)
  const byPeriod = new Map<string, Record<string, string | number>>()
  for (const row of rows) {
    const point = byPeriod.get(row.period) || { period: row.period }
    point[row.ins_name] = Number(point[row.ins_name] || 0) + row.amount
    byPeriod.set(row.period, point)
  }
  const points = [...byPeriod.values()].sort((a, b) => String(a.period).localeCompare(String(b.period)))
  return { names, points }
}
