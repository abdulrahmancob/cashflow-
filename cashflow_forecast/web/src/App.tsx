import { useCallback, useEffect, useMemo, useState } from 'react'
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
import { api, money, monthLastDay, type Filters } from './api'
import './index.css'

type Tab = 'mission' | 'cash' | 'insights' | 'drill'

const emptyFilters: Filters = {
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

/** Pilot window: Jan → Aug 2026 (extracted Jan–May + recon Jun–Jul + forward Aug). */
const MONTH_LABELS: Record<string, string> = {
  '2026-01': 'Jan',
  '2026-02': 'Feb',
  '2026-03': 'Mar',
  '2026-04': 'Apr',
  '2026-05': 'May',
  '2026-06': 'Jun',
  '2026-07': 'Jul',
  '2026-08': 'Aug',
}
const MONTH_KEYS = Object.keys(MONTH_LABELS)
const WINDOW_START = '2026-01-01'
const WINDOW_END = '2026-08-31'

function CheckList({
  options,
  value,
  onChange,
  maxHeight = 140,
}: {
  options: string[]
  value: string[]
  onChange: (v: string[]) => void
  maxHeight?: number
}) {
  const toggle = (opt: string) => {
    if (value.includes(opt)) onChange(value.filter((x) => x !== opt))
    else onChange([...value, opt])
  }
  return (
    <div className="checklist" style={{ maxHeight }}>
      {options.map((o) => {
        const label = o || '(blank)'
        const key = o || '__blank__'
        return (
          <label key={key} className="check-row">
            <input
              type="checkbox"
              checked={value.includes(o)}
              onChange={() => toggle(o)}
            />
            <span title={label}>{label}</span>
          </label>
        )
      })}
    </div>
  )
}

function DataTable({ rows }: { rows: Array<Record<string, unknown>> }) {
  if (!rows.length) return <p className="sub">No rows</p>
  const cols = Object.keys(rows[0]).slice(0, 12)
  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {cols.map((c) => (
                <td key={c}>{String(r[c] ?? '')}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function App() {
  const [tab, setTab] = useState<Tab>('mission')
  const [filterOpts, setFilterOpts] = useState({
    facilities: [] as string[],
    insurers: [] as string[],
    stages: [] as string[],
    risk_flags: [] as string[],
    months: [] as string[],
    date_min: WINDOW_START,
    date_max: WINDOW_END,
    severities: ['error', 'warning'],
  })
  const [filters, setFilters] = useState<Filters>(emptyFilters)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const [kpi, setKpi] = useState<Record<string, unknown>>({})
  const [summary, setSummary] = useState<Awaited<ReturnType<typeof api.outcomesSummary>> | null>(
    null,
  )
  const [monthly, setMonthly] = useState<Array<{ period: string; amount: number }>>([])
  const [actual, setActual] = useState<Array<{ period: string; amount: number }>>([])
  const [byFac, setByFac] = useState<Array<{ facility_name: string; amount: number }>>([])
  const [byIns, setByIns] = useState<Array<{ ins_name: string; amount: number }>>([])
  const [grain, setGrain] = useState<'Monthly' | 'Daily'>('Monthly')
  const [dailyProj, setDailyProj] = useState<Array<{ period: string; amount: number }>>([])
  const [insights, setInsights] = useState<Awaited<ReturnType<typeof api.insights>> | null>(null)
  const [drillOut, setDrillOut] = useState<Array<Record<string, unknown>>>([])
  const [drillRisk, setDrillRisk] = useState<Array<Record<string, unknown>>>([])
  const [drillCpt, setDrillCpt] = useState<Array<Record<string, unknown>>>([])
  const [drillIcd, setDrillIcd] = useState<Array<Record<string, unknown>>>([])

  const filterKey = useMemo(() => JSON.stringify(filters), [filters])
  const hasDayRange = Boolean(filters.dateFrom || filters.dateTo)
  const effectiveGrain = hasDayRange ? 'Daily' : grain

  useEffect(() => {
    api
      .filters()
      .then((f) => {
        setFilterOpts({
          ...f,
          // Clamp picker to Jan–Aug pilot window
          date_min: WINDOW_START,
          date_max: WINDOW_END,
          months: MONTH_KEYS,
        })
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  useEffect(() => {
    if (hasDayRange && grain !== 'Daily') setGrain('Daily')
  }, [hasDayRange, grain])

  const refreshCore = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [k, s, m, a, bf, bi] = await Promise.all([
        api.kpi(filters),
        api.outcomesSummary(filters),
        api.projectedMonthly(filters),
        api.actualDaily(filters),
        api.projectedByFacility(filters),
        api.projectedByInsurance(filters),
      ])
      setKpi(k)
      setSummary(s)
      setMonthly(m)
      setActual(a)
      setByFac(bf)
      setByIns(bi)
    } catch (e) {
      setError((e as Error).message + ' — is the API running on :8787?')
    } finally {
      setLoading(false)
    }
  }, [filterKey]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void refreshCore()
  }, [refreshCore])

  useEffect(() => {
    if (tab === 'cash' && effectiveGrain === 'Daily') {
      api.projectedDaily(filters).then(setDailyProj).catch(() => undefined)
    }
  }, [tab, effectiveGrain, filterKey]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (tab === 'insights') {
      api.insights(filters).then(setInsights).catch((e: Error) => setError(e.message))
    }
  }, [tab, filterKey]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (tab === 'drill') {
      Promise.all([
        api.drillOutcomes(filters),
        api.drillRisk(filters),
        api.drillCpt(filters),
        api.drillIcd(filters),
      ])
        .then(([o, r, c, i]) => {
          setDrillOut(o)
          setDrillRisk(r)
          setDrillCpt(c)
          setDrillIcd(i)
        })
        .catch((e: Error) => setError(e.message))
    }
  }, [tab, filterKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const patch = (partial: Partial<Filters>) => setFilters((f) => ({ ...f, ...partial }))
  const clearFilters = () => {
    setFilters(emptyFilters)
    setGrain('Monthly')
  }

  const setMonthShortcut = (ym: string) => {
    const from = `${ym}-01`
    const to = monthLastDay(ym)
    const active =
      filters.dateFrom === from && filters.dateTo === to && filters.month.length === 0
    if (active) {
      patch({ dateFrom: '', dateTo: '', month: [] })
      return
    }
    patch({ dateFrom: from, dateTo: to, month: [] })
  }

  const activeChips: string[] = []
  filters.facility.forEach((f) => activeChips.push(`Clinic: ${f || '(blank)'}`))
  filters.ins.forEach((f) => activeChips.push(`Ins: ${f}`))
  filters.stage.forEach((f) => activeChips.push(`Stage: ${f}`))
  if (filters.dateFrom || filters.dateTo) {
    activeChips.push(
      `Dates: ${filters.dateFrom || '…'} → ${filters.dateTo || '…'}`,
    )
  } else {
    filters.month.forEach((f) => activeChips.push(`Month: ${f}`))
  }
  filters.severity.forEach((f) => activeChips.push(`Severity: ${f}`))

  return (
    <>
      <aside className="sidebar">
        <h2>Navigation array</h2>
        <button type="button" className="clear-btn" onClick={clearFilters}>
          Clear filters
        </button>
        <label>Clinic {filters.facility.length ? `(${filters.facility.length})` : '(all)'}</label>
        <CheckList
          options={filterOpts.facilities}
          value={filters.facility}
          onChange={(facility) => patch({ facility })}
        />
        <label>Insurance {filters.ins.length ? `(${filters.ins.length})` : '(all)'}</label>
        <CheckList
          options={filterOpts.insurers}
          value={filters.ins}
          onChange={(ins) => patch({ ins })}
          maxHeight={160}
        />
        <label>Outcome stage {filters.stage.length ? `(${filters.stage.length})` : '(all)'}</label>
        <CheckList
          options={filterOpts.stages}
          value={filters.stage}
          onChange={(stage) => patch({ stage })}
          maxHeight={120}
        />
        <label>Date from {filters.dateFrom ? '' : '(all)'}</label>
        <input
          type="date"
          value={filters.dateFrom}
          min={filterOpts.date_min}
          max={filterOpts.date_max}
          onChange={(e) => patch({ dateFrom: e.target.value, month: [] })}
        />
        <label>Date to {filters.dateTo ? '' : '(all)'}</label>
        <input
          type="date"
          value={filters.dateTo}
          min={filterOpts.date_min}
          max={filterOpts.date_max}
          onChange={(e) => patch({ dateTo: e.target.value, month: [] })}
        />
        <label>Month shortcuts</label>
        <div className="month-chips">
          {(filterOpts.months.length ? filterOpts.months : MONTH_KEYS).map((ym) => {
            const from = `${ym}-01`
            const to = monthLastDay(ym)
            const active = filters.dateFrom === from && filters.dateTo === to
            return (
              <button
                key={ym}
                type="button"
                className={`month-chip${active ? ' active' : ''}`}
                onClick={() => setMonthShortcut(ym)}
              >
                {MONTH_LABELS[ym] || ym}
              </button>
            )
          })}
        </div>
        <label>
          Audit severity {filters.severity.length ? `(${filters.severity.length})` : '(all)'}
        </label>
        <CheckList
          options={filterOpts.severities}
          value={filters.severity}
          onChange={(severity) => patch({ severity })}
          maxHeight={80}
        />
        <label>Search (drill)</label>
        <input
          type="text"
          value={filters.q}
          placeholder="patient / payor / clinic"
          onChange={(e) => patch({ q: e.target.value })}
        />
        <p className="caption" style={{ marginTop: '1rem' }}>
          Click checkboxes to filter. Empty = all.
        </p>
      </aside>

      <main className="main">
        <h1 className="mission-title">CASH CONVERSION CYCLE</h1>
        <p className="caption">
          Orbital forecast · React · as-of {String(kpi.as_of ?? '—')} · outcome ≠ risk
          {kpi.filtered ? ' · filters active' : ''}
        </p>

        {activeChips.length > 0 && (
          <div className="chips">
            {activeChips.map((c) => (
              <span key={c} className="chip">
                {c}
              </span>
            ))}
          </div>
        )}

        <div className="tabs">
          {(
            [
              ['mission', 'Mission Control'],
              ['cash', 'Cash Trajectory'],
              ['insights', 'Business Insights'],
              ['drill', 'Drill Decks'],
            ] as const
          ).map(([id, label]) => (
            <button
              key={id}
              type="button"
              className={tab === id ? 'active' : ''}
              onClick={() => setTab(id)}
            >
              {label}
            </button>
          ))}
        </div>

        {error && <div className="error-banner">{error}</div>}
        {loading && tab === 'mission' && <div className="loading">Syncing orbital data…</div>}

        {tab === 'mission' && !loading && (
          <>
            <section className="glass">
              <h3>Outcome — cash flow summary</h3>
              <p className="sub">Realized collection states — not predictive risk flags.</p>
              <div className="kpi-row">
                <div className="kpi">
                  <div className="label">
                    {kpi.filtered ? 'Actual remits' : 'Actual received'}
                  </div>
                  <div className="value">
                    {money(
                      kpi.filtered ? kpi.actual_cash_received_filtered : kpi.actual_cash_received,
                    )}
                  </div>
                  {kpi.filtered ? (
                    <div className="delta">Global remit: {money(kpi.actual_cash_received)}</div>
                  ) : null}
                </div>
                <div className="kpi">
                  <div className="label">
                    {kpi.filtered ? 'Expected land' : 'Projected AR stock'}
                  </div>
                  <div className="value">{money(kpi.projected_cash_in)}</div>
                  <div className="delta">
                    {kpi.filtered
                      ? 'Based on open on-track and overdue AR. Excludes patient payments.'
                      : 'Open on-track + overdue (not a single day)'}
                  </div>
                </div>
                <div className="kpi">
                  <div className="label">On track</div>
                  <div className="value">{money(kpi.on_track_amount)}</div>
                  <div className="delta">{Number(kpi.on_track_count ?? 0).toLocaleString()} lines</div>
                </div>
                <div className="kpi">
                  <div className="label">Overdue</div>
                  <div className="value">{money(kpi.overdue_amount)}</div>
                  <div className="delta">{Number(kpi.overdue_count ?? 0).toLocaleString()} lines</div>
                </div>
                <div className="kpi">
                  <div className="label">Denied+reject</div>
                  <div className="value">{money(kpi.denied_amount)}</div>
                  <div className="delta">{Number(kpi.denied_count ?? 0).toLocaleString()} lines</div>
                </div>
                <div className="kpi">
                  <div className="label">
                    {hasDayRange ? 'Cash land in range' : 'Jan–Aug proj'}
                  </div>
                  <div className="value">{money(kpi.projected_cash_may_aug)}</div>
                  {hasDayRange ? (
                    <div className="delta">Same stages as Expected land (excludes denied)</div>
                  ) : null}
                </div>
              </div>
            </section>

            <section className="risk-glass">
              <h3>Risk exposure — predictive</h3>
              <p className="sub">Audit / unsubmitted flags are separate from outcome stages.</p>
              <div className="kpi-row">
                <div className="kpi">
                  <div className="label">Risk exposure</div>
                  <div className="value">{money(kpi.risk_exposure_amount)}</div>
                </div>
                <div className="kpi">
                  <div className="label">Visits with risk</div>
                  <div className="value">{Number(kpi.risk_visit_count ?? 0).toLocaleString()}</div>
                </div>
              </div>
              {summary?.risk_by_flag?.length ? (
                <DataTable rows={summary.risk_by_flag as Array<Record<string, unknown>>} />
              ) : null}
            </section>

            <div className="grid-2">
              <section className="glass">
                <h3>Outcome stage distribution</h3>
                <div className="chart-box">
                  <ResponsiveContainer>
                    <BarChart data={summary?.stages ?? []}>
                      <CartesianGrid stroke="#1a3344" strokeDasharray="3 3" />
                      <XAxis dataKey="outcome_stage" stroke="#9bb8c9" tick={{ fontSize: 11 }} />
                      <YAxis stroke="#9bb8c9" tick={{ fontSize: 11 }} />
                      <Tooltip
                        contentStyle={{ background: '#0b1824', border: '1px solid #1a4a5c' }}
                      />
                      <Bar dataKey="line_count" fill="#4aa8e8" radius={[4, 4, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </section>
              <section className="glass">
                <h3>Top overdue by insurance</h3>
                <div className="chart-box">
                  <ResponsiveContainer>
                    <BarChart
                      layout="vertical"
                      data={(summary?.overdue_by_insurance ?? []).slice(0, 12)}
                      margin={{ left: 20 }}
                    >
                      <CartesianGrid stroke="#1a3344" strokeDasharray="3 3" />
                      <XAxis type="number" stroke="#9bb8c9" tick={{ fontSize: 11 }} />
                      <YAxis
                        type="category"
                        dataKey="ins_name"
                        width={120}
                        stroke="#9bb8c9"
                        tick={{ fontSize: 10 }}
                      />
                      <Tooltip
                        contentStyle={{ background: '#0b1824', border: '1px solid #1a4a5c' }}
                      />
                      <Bar dataKey="expected_payment" fill="#e8b84a" radius={[0, 4, 4, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </section>
            </div>
          </>
        )}

        {tab === 'cash' && (
          <>
            <div className="tabs">
              <button
                type="button"
                className={effectiveGrain === 'Monthly' ? 'active' : ''}
                onClick={() => setGrain('Monthly')}
                disabled={hasDayRange}
                title={hasDayRange ? 'Clear date range to use monthly grain' : undefined}
              >
                Monthly
              </button>
              <button
                type="button"
                className={effectiveGrain === 'Daily' ? 'active' : ''}
                onClick={() => setGrain('Daily')}
              >
                Daily
              </button>
            </div>
            <div className="grid-2">
              <section className="glass">
                <h3>Projected cash-in</h3>
                <div className="chart-box">
                  <ResponsiveContainer>
                    {effectiveGrain === 'Monthly' ? (
                      <BarChart data={monthly}>
                        <CartesianGrid stroke="#1a3344" strokeDasharray="3 3" />
                        <XAxis dataKey="period" stroke="#9bb8c9" />
                        <YAxis stroke="#9bb8c9" />
                        <Tooltip
                          contentStyle={{ background: '#0b1824', border: '1px solid #1a4a5c' }}
                        />
                        <Bar dataKey="amount" fill="#2ec4c6" radius={[4, 4, 0, 0]} />
                      </BarChart>
                    ) : (
                      <LineChart data={dailyProj}>
                        <CartesianGrid stroke="#1a3344" strokeDasharray="3 3" />
                        <XAxis dataKey="period" hide />
                        <YAxis stroke="#9bb8c9" />
                        <Tooltip
                          contentStyle={{ background: '#0b1824', border: '1px solid #1a4a5c' }}
                        />
                        <Line type="monotone" dataKey="amount" stroke="#7fffff" dot={false} />
                      </LineChart>
                    )}
                  </ResponsiveContainer>
                </div>
              </section>
              <section className="glass">
                <h3>Actual cash by check date</h3>
                <div className="chart-box">
                  <ResponsiveContainer>
                    <LineChart data={actual}>
                      <CartesianGrid stroke="#1a3344" strokeDasharray="3 3" />
                      <XAxis dataKey="period" hide />
                      <YAxis stroke="#9bb8c9" />
                      <Tooltip
                        contentStyle={{ background: '#0b1824', border: '1px solid #1a4a5c' }}
                      />
                      <Line type="monotone" dataKey="amount" stroke="#4aa8e8" dot={false} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              </section>
            </div>
            <div className="grid-2">
              <section className="glass">
                <h3>Projected by clinic</h3>
                <div className="chart-box">
                  <ResponsiveContainer>
                    <BarChart layout="vertical" data={byFac.slice(0, 12)} margin={{ left: 10 }}>
                      <CartesianGrid stroke="#1a3344" strokeDasharray="3 3" />
                      <XAxis type="number" stroke="#9bb8c9" />
                      <YAxis
                        type="category"
                        dataKey="facility_name"
                        width={100}
                        stroke="#9bb8c9"
                        tick={{ fontSize: 10 }}
                      />
                      <Tooltip
                        contentStyle={{ background: '#0b1824', border: '1px solid #1a4a5c' }}
                      />
                      <Bar dataKey="amount" fill="#3dcf8e" />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </section>
              <section className="glass">
                <h3>Projected by insurance</h3>
                <div className="chart-box">
                  <ResponsiveContainer>
                    <BarChart layout="vertical" data={byIns.slice(0, 12)} margin={{ left: 10 }}>
                      <CartesianGrid stroke="#1a3344" strokeDasharray="3 3" />
                      <XAxis type="number" stroke="#9bb8c9" />
                      <YAxis
                        type="category"
                        dataKey="ins_name"
                        width={120}
                        stroke="#9bb8c9"
                        tick={{ fontSize: 10 }}
                      />
                      <Tooltip
                        contentStyle={{ background: '#0b1824', border: '1px solid #1a4a5c' }}
                      />
                      <Bar dataKey="amount" fill="#4aa8e8" />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </section>
            </div>
            {summary?.sla?.length ? (
              <section className="glass">
                <h3>Payer SLA</h3>
                <DataTable rows={summary.sla} />
              </section>
            ) : null}
          </>
        )}

        {tab === 'insights' && insights && (
          <>
            <section className="glass">
              <h3>Business insights — billing audit</h3>
              <p className="sub">
                Predictive CPT/ICD signals from audit — not cash outcome stages.
              </p>
              {insights.cards.map((c) => (
                <div key={c.title} className={`insight-card ${c.tone}`}>
                  <h4>{c.title}</h4>
                  <p>{c.body}</p>
                </div>
              ))}
              <div className="insight-card warn">
                <h4>Linked forecast risk exposure</h4>
                <p>
                  audit_cpt / audit_icd:{' '}
                  <strong>{money(insights.audit_risk_exposure)}</strong> across{' '}
                  {insights.audit_risk_visits.toLocaleString()} visits.
                </p>
              </div>
            </section>
            <div className="grid-2">
              <section className="glass">
                <h3>Top CPT violation rules</h3>
                <div className="chart-box">
                  <ResponsiveContainer>
                    <BarChart layout="vertical" data={insights.top_cpt_rules} margin={{ left: 10 }}>
                      <CartesianGrid stroke="#1a3344" strokeDasharray="3 3" />
                      <XAxis type="number" stroke="#9bb8c9" />
                      <YAxis
                        type="category"
                        dataKey="rule_id"
                        width={140}
                        stroke="#9bb8c9"
                        tick={{ fontSize: 10 }}
                      />
                      <Tooltip
                        contentStyle={{ background: '#0b1824', border: '1px solid #1a4a5c' }}
                      />
                      <Bar dataKey="count" fill="#e87450" />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </section>
              <section className="glass">
                <h3>ICD conflict categories</h3>
                <div className="chart-box">
                  <ResponsiveContainer>
                    <BarChart layout="vertical" data={insights.icd_categories} margin={{ left: 10 }}>
                      <CartesianGrid stroke="#1a3344" strokeDasharray="3 3" />
                      <XAxis type="number" stroke="#9bb8c9" />
                      <YAxis
                        type="category"
                        dataKey="category"
                        width={140}
                        stroke="#9bb8c9"
                        tick={{ fontSize: 10 }}
                      />
                      <Tooltip
                        contentStyle={{ background: '#0b1824', border: '1px solid #1a4a5c' }}
                      />
                      <Bar dataKey="count" fill="#e8b84a" />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </section>
            </div>
            <section className="glass">
              <h3>Clinic × severity (CPT)</h3>
              <DataTable rows={insights.facility_severity} />
            </section>
            <section className="glass">
              <h3>ICD guidance samples</h3>
              <DataTable rows={insights.icd_guidance} />
            </section>
            <section className="glass">
              <h3>Unmapped insurance</h3>
              <DataTable rows={insights.unmapped} />
            </section>
          </>
        )}

        {tab === 'drill' && (
          <>
            <section className="glass">
              <h3>Outcome lines</h3>
              <DataTable rows={drillOut} />
            </section>
            <section className="glass">
              <h3>Risk flags</h3>
              <DataTable rows={drillRisk} />
            </section>
            <section className="glass">
              <h3>CPT violations</h3>
              <DataTable rows={drillCpt} />
            </section>
            <section className="glass">
              <h3>ICD violations</h3>
              <DataTable rows={drillIcd} />
            </section>
          </>
        )}
      </main>
    </>
  )
}
