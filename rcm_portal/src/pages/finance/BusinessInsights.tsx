import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { forecastApi, money, type ExecScorecard, type Filters } from '../../api/forecast'
import { cptAuditApi } from '../../api/cptAudit'
import { useAuth } from '../../auth/AuthContext'
import {
  ArTrendChart,
  CollectionRateChart,
  LeakageWaterfall,
  PayerScorecardTable,
  RecoveryBars,
  ScorecardTiles,
} from '../../components/finance/ExecScorecard'
import { AUDIT_DOMAIN_LABEL, buildExecStoryCards, MoneyTooltip } from '../../components/finance/shared'
import { EmptyState } from '../../components/table'
import { Alert, Card, KpiCard } from '../../components/ui'

type OutcomesSummary = Awaited<ReturnType<typeof forecastApi.outcomesSummary>>

export function BusinessInsights({
  filters,
  onError,
}: {
  filters: Filters
  onError: (message: string) => void
}) {
  const { hasRole } = useAuth()
  const [kpi, setKpi] = useState<Record<string, number | string | boolean>>({})
  const [summary, setSummary] = useState<OutcomesSummary | null>(null)
  const [scorecard, setScorecard] = useState<ExecScorecard | null>(null)
  const [cptInsights, setCptInsights] = useState<{
    insights_published: boolean
    headline: Record<string, number | null>
    secondary: Record<string, number>
    missing_90901_by_month: Array<Record<string, unknown>>
  } | null>(null)
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
          const [cpt, outcomes, k, exec] = await Promise.all([
            cptAuditApi.insights().catch(() => null),
            forecastApi.outcomesSummary(filters).catch(() => null),
            forecastApi.kpi(filters).catch(() => ({})),
            forecastApi.execScorecard(filters).catch(() => null),
          ])
          if (!cancelled) {
            setCptInsights(cpt)
            setSummary(outcomes)
            setKpi(k)
            setScorecard(exec)
          }
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
  const storyCards = useMemo(
    () => buildExecStoryCards(summary, kpi, scorecard?.narrative),
    [summary, kpi, scorecard],
  )

  return (
    <div className="space-y-4">
      <ScorecardTiles data={scorecard} />
      <div className="grid gap-4 md:grid-cols-2">
        {storyCards.map((c) => (
          <Card key={c.title} title={c.title}>
            <p className="text-sm text-gray-600 dark:text-gray-300">{c.body}</p>
          </Card>
        ))}
        {!storyCards.length && (
          <Card>
            <EmptyState title="No live insight cards" description="No Eligibility Sheet paid, overdue, or open audit-queue risk for the current filters." />
          </Card>
        )}
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <ArTrendChart data={scorecard} />
        <CollectionRateChart data={scorecard} />
      </div>
      <LeakageWaterfall data={scorecard} />
      <PayerScorecardTable data={scorecard} />
      <div className="grid gap-4 lg:grid-cols-2">
        <RecoveryBars data={scorecard} />
        <Card title="Documentation risk by flag" description="Open CPT / ICD-10 / Demographics audit risk plus Collection Denied charged — not warnings" padded={false}>
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
                <EmptyState title="No open audit-queue dollars" description="No open CPT / ICD-10 / Demographics risk after excluding Eligibility Sheet paid status." />
              </div>
            )}
          </div>
        </Card>
      </div>

      <div className="space-y-4">
        <h2 className="font-display text-lg font-semibold text-gray-900 dark:text-white">Leakage detail</h2>
        <div className="grid gap-4 lg:grid-cols-2">
          <Card
            title="CPT cash at risk"
            description={cptInsights && !cptInsights.insights_published ? 'Headlines unpublished pending review — not shown as fact' : 'Paid vs open CPT audit exposure'}
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
                CPT headline totals are unpublished pending sample review. Secondary counts are visible; dollar headlines stay hidden.
              </Alert>
            )}
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <KpiCard
                label="Identified opportunity (high+medium)"
                value={cptInsights.insights_published ? money(cptInsights.headline.opportunity_high_medium) : 'Unpublished'}
              />
              <KpiCard
                label="At-risk paid exposure"
                value={cptInsights.insights_published ? money(cptInsights.headline.at_risk_paid) : 'Unpublished'}
              />
              <KpiCard
                label="Open at-risk exposure"
                value={cptInsights.insights_published ? money(cptInsights.headline.open_at_risk) : 'Unpublished'}
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
    </div>
  )
}
