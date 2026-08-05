import { api } from './client'

export type Filters = {
  facility: string[]
  ins: string[]
  stage: string[]
  month: string[]
  dateFrom: string
  dateTo: string
  severity: string[]
  riskFlag: string[]
  q: string
}

function qs(filters: Partial<Filters>, extra: Record<string, string | number | undefined> = {}) {
  const p = new URLSearchParams()
  if (filters.facility?.length) p.set('facility', filters.facility.join(','))
  if (filters.ins?.length) p.set('ins', filters.ins.join(','))
  if (filters.stage?.length) p.set('stage', filters.stage.join(','))
  if (filters.dateFrom) p.set('date_from', filters.dateFrom)
  if (filters.dateTo) p.set('date_to', filters.dateTo)
  if (!filters.dateFrom && !filters.dateTo && filters.month?.length) {
    p.set('month', filters.month.join(','))
  }
  if (filters.severity?.length) p.set('severity', filters.severity.join(','))
  if (filters.riskFlag?.length) p.set('risk_flag', filters.riskFlag.join(','))
  if (filters.q) p.set('q', filters.q)
  for (const [k, v] of Object.entries(extra)) {
    if (v !== undefined && v !== '') p.set(k, String(v))
  }
  const s = p.toString()
  return s ? `?${s}` : ''
}

export const forecastApi = {
  filters: () =>
    api<{
      facilities: string[]
      insurers: string[]
      stages: string[]
      risk_flags: string[]
      months: string[]
      date_min: string
      date_max: string
      severities: string[]
    }>(`/api/meta/filters`),
  kpi: (f: Partial<Filters>) =>
    api<Record<string, number | string | boolean>>(`/api/kpi${qs(f)}`),
  projectedMonthly: (f: Partial<Filters>) =>
    api<Array<{ period: string; amount: number }>>(`/api/projected/monthly${qs(f)}`),
  projectedDaily: (f: Partial<Filters> = {}) =>
    api<Array<{ period: string; amount: number }>>(`/api/projected/daily${qs(f)}`),
  projectedByFacility: (f: Partial<Filters>) =>
    api<Array<{ facility_name: string; amount: number }>>(`/api/projected/by-facility${qs(f)}`),
  actualDaily: (f: Partial<Filters> = {}) =>
    api<Array<{ period: string; amount: number }>>(`/api/actual/daily${qs(f)}`),
  outcomesSummary: (f: Partial<Filters>) =>
    api<{
      stages: Array<{ outcome_stage: string; line_count: number; amount: number }>
      risk_by_flag: Array<{ risk_flag: string; exposure_amount: number }>
    }>(`/api/outcomes/summary${qs(f)}`),
  insights: (f: Partial<Filters>) =>
    api<{
      cards: Array<{ title: string; body: string; tone: string }>
      top_cpt_rules: Array<{ rule_id: string; count: number; error_share: number }>
    }>(`/api/insights${qs(f)}`),
  drillOutcomes: (f: Partial<Filters>, limit = 200) =>
    api<Array<Record<string, unknown>>>(`/api/drill/outcomes${qs(f, { limit })}`),
}

export function money(v: unknown): string {
  const n = Number(v ?? 0)
  return Number.isFinite(n)
    ? `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
    : '$0'
}
