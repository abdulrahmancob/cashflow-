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
  // Day range takes precedence over month checkboxes (API also prefers date_from/to)
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

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${res.status} ${path}`)
  return res.json() as Promise<T>
}

export const api = {
  health: () => get<{ status: string }>('/api/health'),
  filters: () =>
    get<{
      facilities: string[]
      insurers: string[]
      stages: string[]
      risk_flags: string[]
      months: string[]
      date_min: string
      date_max: string
      severities: string[]
    }>('/api/meta/filters'),
  kpi: (f: Partial<Filters>) => get<Record<string, number | string | boolean>>(`/api/kpi${qs(f)}`),
  projectedMonthly: (f: Partial<Filters>) =>
    get<Array<{ period: string; amount: number }>>(`/api/projected/monthly${qs(f)}`),
  projectedDaily: (f: Partial<Filters> = {}) =>
    get<Array<{ period: string; amount: number }>>(`/api/projected/daily${qs(f)}`),
  projectedByFacility: (f: Partial<Filters>) =>
    get<Array<{ facility_name: string; amount: number }>>(`/api/projected/by-facility${qs(f)}`),
  projectedByInsurance: (f: Partial<Filters>) =>
    get<Array<{ ins_name: string; amount: number }>>(`/api/projected/by-insurance${qs(f)}`),
  actualDaily: (f: Partial<Filters> = {}) =>
    get<Array<{ period: string; amount: number }>>(`/api/actual/daily${qs(f)}`),
  outcomesSummary: (f: Partial<Filters>) =>
    get<{
      stages: Array<{ outcome_stage: string; line_count: number; amount: number }>
      risk_by_flag: Array<{ risk_flag: string; exposure_amount: number }>
      overdue_by_insurance: Array<Record<string, unknown>>
      sla: Array<Record<string, unknown>>
    }>(`/api/outcomes/summary${qs(f)}`),
  insights: (f: Partial<Filters>) =>
    get<{
      cards: Array<{ title: string; body: string; tone: string }>
      top_cpt_rules: Array<{ rule_id: string; count: number; error_share: number }>
      icd_categories: Array<{ category: string; count: number }>
      facility_severity: Array<Record<string, unknown>>
      icd_guidance: Array<Record<string, unknown>>
      unmapped: Array<Record<string, unknown>>
      audit_risk_exposure: number
      audit_risk_visits: number
    }>(`/api/insights${qs(f)}`),
  drillOutcomes: (f: Partial<Filters>, limit = 400) =>
    get<Array<Record<string, unknown>>>(`/api/drill/outcomes${qs(f, { limit })}`),
  drillRisk: (f: Partial<Filters>, limit = 400) =>
    get<Array<Record<string, unknown>>>(`/api/drill/risk${qs(f, { limit })}`),
  drillCpt: (f: Partial<Filters>, limit = 400) =>
    get<Array<Record<string, unknown>>>(`/api/drill/audit-cpt${qs(f, { limit })}`),
  drillIcd: (f: Partial<Filters>, limit = 400) =>
    get<Array<Record<string, unknown>>>(`/api/drill/audit-icd${qs(f, { limit })}`),
}

export function money(v: unknown): string {
  const n = Number(v ?? 0)
  return Number.isFinite(n) ? `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}` : '$0'
}

/** YYYY-MM → last calendar day as YYYY-MM-DD */
export function monthLastDay(ym: string): string {
  const [y, m] = ym.split('-').map(Number)
  const last = new Date(y, m, 0)
  const mm = String(last.getMonth() + 1).padStart(2, '0')
  const dd = String(last.getDate()).padStart(2, '0')
  return `${last.getFullYear()}-${mm}-${dd}`
}
