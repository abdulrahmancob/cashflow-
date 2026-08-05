import { useEffect, useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { forecastApi, money, type Filters } from '../api/forecast'
import { Card, KpiCard } from '../components/ui'

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

export function FinancePage() {
  const { tab } = useParams()
  const view = tab || 'mission'
  const [filters] = useState<Filters>(empty)
  const [kpi, setKpi] = useState<Record<string, number | string | boolean>>({})
  const [monthly, setMonthly] = useState<Array<{ period: string; amount: number }>>([])
  const [daily, setDaily] = useState<Array<{ period: string; amount: number }>>([])
  const [actual, setActual] = useState<Array<{ period: string; amount: number }>>([])
  const [byFacility, setByFacility] = useState<
    Array<{ facility_name: string; amount: number }>
  >([])
  const [insights, setInsights] = useState<{
    cards: Array<{ title: string; body: string; tone: string }>
  }>({ cards: [] })
  const [drill, setDrill] = useState<Array<Record<string, unknown>>>([])
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        setError('')
        if (view === 'mission') {
          const [k, m, f] = await Promise.all([
            forecastApi.kpi(filters),
            forecastApi.projectedMonthly(filters),
            forecastApi.projectedByFacility(filters),
          ])
          if (!cancelled) {
            setKpi(k)
            setMonthly(m)
            setByFacility(f.slice(0, 12))
          }
        } else if (view === 'cash') {
          const [d, a] = await Promise.all([
            forecastApi.projectedDaily(filters),
            forecastApi.actualDaily(filters),
          ])
          if (!cancelled) {
            setDaily(d)
            setActual(a)
          }
        } else if (view === 'insights') {
          const i = await forecastApi.insights(filters)
          if (!cancelled) setInsights(i)
        } else if (view === 'drill') {
          const rows = await forecastApi.drillOutcomes(filters)
          if (!cancelled) setDrill(rows.slice(0, 100))
        }
      } catch (e) {
        if (!cancelled) setError(String((e as Error).message || e))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [view, filters])

  const cashSeries = useMemo(() => {
    const map = new Map<string, { period: string; projected: number; actual: number }>()
    for (const r of daily) {
      map.set(r.period, { period: r.period, projected: r.amount, actual: 0 })
    }
    for (const r of actual) {
      const cur = map.get(r.period) || { period: r.period, projected: 0, actual: 0 }
      cur.actual = r.amount
      map.set(r.period, cur)
    }
    return [...map.values()].sort((a, b) => a.period.localeCompare(b.period)).slice(-60)
  }, [daily, actual])

  const titles: Record<string, string> = {
    mission: 'Mission Control',
    cash: 'Cash Trajectory',
    insights: 'Business Insights',
    drill: 'Drill Decks',
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="font-display text-2xl font-semibold text-gray-900 dark:text-white">
          {titles[view] || 'Finance'}
        </h1>
        <p className="mt-1 text-sm text-gray-500">
          Read-only finance dashboards inside the Operations Portal.
        </p>
      </div>
      {error && (
        <div className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700 dark:border-rose-900 dark:bg-rose-950 dark:text-rose-200">
          {error}
        </div>
      )}

      {view === 'mission' && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <KpiCard label="Projected AR" value={money(kpi.projected_cash_in)} tone="info" />
            <KpiCard
              label="Actual received"
              value={money(kpi.actual_cash_received_filtered ?? kpi.actual_cash_received)}
              tone="ok"
            />
            <KpiCard label="On track" value={money(kpi.on_track_amount)} />
            <KpiCard label="Overdue" value={money(kpi.overdue_amount)} tone="warn" />
          </div>
          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="Projected by month">
              <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={monthly}>
                    <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
                    <XAxis dataKey="period" />
                    <YAxis />
                    <Tooltip />
                    <Bar dataKey="amount" fill="#2563eb" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </Card>
            <Card title="Projected by facility">
              <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={byFacility} layout="vertical">
                    <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
                    <XAxis type="number" />
                    <YAxis type="category" dataKey="facility_name" width={100} />
                    <Tooltip />
                    <Bar dataKey="amount" fill="#0ea5e9" radius={[0, 4, 4, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </Card>
          </div>
        </>
      )}

      {view === 'cash' && (
        <Card title="Projected vs actual (daily)">
          <div className="h-80">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={cashSeries}>
                <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
                <XAxis dataKey="period" hide />
                <YAxis />
                <Tooltip />
                <Line type="monotone" dataKey="projected" stroke="#2563eb" dot={false} />
                <Line type="monotone" dataKey="actual" stroke="#059669" dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Card>
      )}

      {view === 'insights' && (
        <div className="grid gap-4 md:grid-cols-2">
          {(insights.cards || []).map((c) => (
            <Card key={c.title} title={c.title}>
              <p className="text-sm text-gray-600 dark:text-gray-300">{c.body}</p>
            </Card>
          ))}
          {!insights.cards?.length && (
            <Card>
              <p className="text-sm text-gray-500">No insight cards available.</p>
            </Card>
          )}
        </div>
      )}

      {view === 'drill' && (
        <Card title="Outcome drill (sample)">
          <div className="overflow-x-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="bg-gray-50 text-xs uppercase text-gray-500 dark:bg-gray-800">
                <tr>
                  {Object.keys(drill[0] || {})
                    .slice(0, 8)
                    .map((h) => (
                      <th key={h} className="px-3 py-2">
                        {h}
                      </th>
                    ))}
                </tr>
              </thead>
              <tbody>
                {drill.map((row, i) => (
                  <tr key={i} className="border-t border-gray-100 dark:border-gray-800">
                    {Object.keys(drill[0] || {})
                      .slice(0, 8)
                      .map((h) => (
                        <td key={h} className="px-3 py-2">
                          {String(row[h] ?? '')}
                        </td>
                      ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  )
}
