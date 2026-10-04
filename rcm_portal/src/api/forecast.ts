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

export type OverdueAnalysis = {
  by_month: Array<{ period: string; basis: string; ins_name: string; amount: number; count: number }>
  aging: Array<{
    ins_name: string
    b0_30: number
    b31_60: number
    b61_90: number
    b90_plus: number
    total: number
  }>
  recovery: Array<{ ins_name: string; face: number; expected_recovery: number }>
  pareto: {
    rows: Array<{ ins_name: string; amount: number; share_pct: number; cumulative_pct: number }>
    top3_share: number
    top5_share: number
  }
  days_to_pay: Array<{
    ins_name: string
    amount: number
    claims: number
    avg_overdue_days: number
    avg_sla_lag_days: number | null
    share_90_plus: number
    expected_recovery: number
  }>
  by_clinic: Array<{ facility_name: string; amount: number; count: number; avg_overdue_days: number }>
  chase_list: Array<{
    patient_name: string
    emr_patient_id: string
    dos: string
    facility_name: string
    ins_name: string
    cpt_code: string
    expected_amount: number
    expected_land_date: string
    overdue_days: number
    sla_lag_days: number | null
    recovery_amount: number
  }>
  trend: Array<{ as_of: string; overdue: number; on_track: number; overdue_90_plus: number }>
  kpi: {
    overdue_90_plus: number
    expected_recovery: number
    recovery_pct: number
    face: number
    overdue_change_4w: number | null
    overdue_change_4w_pct: number | null
    top3_share: number
  }
}

export type CashOverview = {
  daily: Array<{ period: string; amount: number; horizon_kind?: string; forecast_as_of?: string | null }>
  monthly: Array<{ period: string; amount: number; forecast_as_of?: string | null }>
  actual: Array<{ period: string; amount: number }>
  last_settled_date: string
  kpi: {
    cash_mtd: number
    forecast_mtd: number
    pace_pct: number | null
    next_10d: number
    next_30d: number
    mae_30d: number | null
    through: string | null
  }
  burnup: Array<{
    period: string
    forecast_cum: number
    actual_cum: number | null
    forecast_to_date: number | null
    forecast_rest: number | null
  }>
  forward_weeks: Array<{ week: string; on_track: number; overdue_recovery: number }>
  accuracy: {
    mae_30d: number | null
    days: Array<{ bank_date: string; error_pct: number }>
    yesterday: {
      bank_date: string
      forecast_total: number
      actual_total: number
      error_pct: number | null
    } | null
    recent: Array<{
      bank_date: string
      forecast_total: number
      actual_total: number
      error_pct: number | null
    }>
  }
  payer_months: Array<{ period: string; ins_name: string; paid_amount: number; check_count?: number }>
}

export type ExecScorecard = {
  tiles: Array<{
    key: string
    label: string
    value: number | null
    unit: 'money' | 'days' | 'pct'
    delta_pct: number | null
    good_when: 'up' | 'down'
    hint: string
  }>
  collection_rate: Array<{
    period: string
    paid: number
    expected: number
    rate_pct: number | null
    maturing: boolean
  }>
  leakage: Array<{ label: string; kind: 'total' | 'loss' | 'result'; amount: number }>
  payer_scorecard: Array<{
    ins_name: string
    cash_90d: number
    revenue_share: number
    collection_rate: number | null
    avg_days: number | null
    overdue: number
    share_90_plus: number
    denied: number
    grade: string
  }>
  trend: OverdueAnalysis['trend']
  recovery: OverdueAnalysis['recovery']
  narrative: {
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
      last_settled_date?: string
      severities: string[]
    }>(`/api/meta/filters`),
  kpi: (f: Partial<Filters>) =>
    api<Record<string, number | string | boolean>>(`/api/kpi${qs(f)}`),
  projectedMonthly: (f: Partial<Filters>) =>
    api<Array<{ period: string; amount: number }>>(`/api/projected/monthly${qs(f)}`),
  projectedHistory: (f: Partial<Filters> = {}) =>
    api<{
      daily: Array<{
        period: string
        amount: number
        forecast_as_of?: string | null
        horizon_kind?: string
      }>
      monthly: Array<{ period: string; amount: number; forecast_as_of?: string | null }>
    }>(`/api/projected/history${qs(f)}`),
  projectedDaily: (f: Partial<Filters> = {}) =>
    api<Array<{ period: string; amount: number; horizon_kind?: string }>>(
      `/api/projected/daily${qs(f)}`,
    ),
  projectedByFacility: (f: Partial<Filters>) =>
    api<Array<{ facility_name: string; amount: number }>>(`/api/projected/by-facility${qs(f)}`),
  projectedByInsurance: (f: Partial<Filters>) =>
    api<Array<{ ins_name: string; amount: number }>>(`/api/projected/by-insurance${qs(f)}`),
  actualDaily: (f: Partial<Filters> = {}) =>
    api<Array<{ period: string; amount: number }>>(`/api/actual/daily${qs(f)}`),
  outcomesSummary: (f: Partial<Filters>) =>
    api<{
      stages: Array<{ outcome_stage: string; line_count: number; amount: number; share_pct?: number }>
      risk_by_flag: Array<{ risk_flag: string; exposure_amount: number }>
      overdue_by_insurance: Array<{
        ins_name: string
        expected_payment: number
        avg_overdue_days?: number
        line_count: number
        share_pct?: number
      }>
      risk_by_insurance: Array<{
        ins_name: string
        exposure_amount: number
        visit_count?: number
        share_pct?: number
      }>
      insurance_mix: Array<{
        ins_name: string
        landed: number
        overdue: number
        risk: number
      }>
      sla: Array<Record<string, unknown>>
    }>(`/api/outcomes/summary${qs(f)}`),
  behaviorTrend: (f: Partial<Filters>, grain: 'month' | 'year' | 'day' = 'month') =>
    api<{
      grain: string
      series: Array<{
        period: string
        ins_name: string
        paid_amount: number
        check_count: number
        median_lag_days?: number | null
      }>
      insurers: string[]
      checks: Array<{
        txn_date: string
        ins_name: string
        check_num: string
        paid_amount: number
      }>
    }>(`/api/behavior/trend${qs(f, { grain })}`),
  mission: (f: Partial<Filters>) =>
    api<{
      kpi: Record<string, number | string | boolean>
      monthly: Array<{ period: string; amount: number }>
      by_facility: Array<{ facility_name: string; amount: number }>
      by_insurance: Array<{ ins_name: string; amount: number }>
      outcomes: {
        stages: Array<{ outcome_stage: string; line_count: number; amount: number; share_pct?: number }>
        risk_by_flag: Array<{ risk_flag: string; exposure_amount: number }>
        overdue_by_insurance: Array<{
          ins_name: string
          expected_payment: number
          avg_overdue_days?: number
          line_count: number
          share_pct?: number
        }>
        risk_by_insurance: Array<{
          ins_name: string
          exposure_amount: number
          visit_count?: number
          share_pct?: number
        }>
        insurance_mix: Array<{
          ins_name: string
          landed: number
          overdue: number
          risk: number
        }>
        sla: Array<Record<string, unknown>>
      }
      behavior: {
        grain: string
        series: Array<{
          period: string
          ins_name: string
          paid_amount: number
          check_count: number
          median_lag_days?: number | null
        }>
        insurers: string[]
        checks: Array<{
          txn_date: string
          ins_name: string
          check_num: string
          paid_amount: number
        }>
      } | null
      day_ahead?: {
        yesterday: {
          bank_date: string
          forecast_total: number
          actual_total: number
          error_pct: number | null
        } | null
        recent: Array<{
          bank_date: string
          forecast_total: number
          actual_total: number
          error_pct: number | null
        }>
      }
      overdue_analysis?: OverdueAnalysis
    }>(`/api/mission${qs(f, { grain: 'day' })}`),
  cashOverview: (f: Partial<Filters>) => api<CashOverview>(`/api/cash/overview${qs(f)}`),
  overdueAnalysis: (f: Partial<Filters>) => api<OverdueAnalysis>(`/api/overdue/analysis${qs(f)}`),
  execScorecard: (f: Partial<Filters>) => api<ExecScorecard>(`/api/exec/scorecard${qs(f)}`),
  insights: (f: Partial<Filters>) =>
    api<{
      cards: Array<{ title: string; body: string; tone: string }>
      top_cpt_rules: Array<{ rule_id: string; count: number; error_share: number }>
    }>(`/api/insights${qs(f)}`),
  drillOutcomes: (f: Partial<Filters>, limit = 200) =>
    api<Array<Record<string, unknown>>>(`/api/drill/outcomes${qs(f, { limit })}`),
  overdueClaims: (f: Partial<Filters>, limit = 500) =>
    api<
      Array<{
        patient_name: string
        emr_patient_id: string
        dos: string
        facility_name: string
        ins_name: string
        cpt_code: string
        expected_amount: number
        expected_land_date: string
        overdue_days: number
        sla_lag_days: number | null
      }>
    >(`/api/overdue/claims${qs(f, { limit })}`),
  unbanked: () =>
    api<{
      waystar_missing: Array<{
        check_number: string
        payer: string
        claim_count: number
        amount: number
        latest_date: string | null
      }>
      tracker_missing: Array<{
        row_id: string
        txn_date: string | null
        check_number: string
        description: string
        transaction_type: string
        amount: number
      }>
    }>('/api/unbanked'),
}

export function money(v: unknown): string {
  const n = Number(v ?? 0)
  return Number.isFinite(n)
    ? `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    : '$0.00'
}

export function pct(part: unknown, whole: unknown): string {
  const a = Number(part ?? 0)
  const b = Number(whole ?? 0)
  if (!Number.isFinite(a) || !Number.isFinite(b) || b === 0) return '0.0%'
  return `${((a / b) * 100).toFixed(1)}%`
}

export function sharePct(v: unknown): string {
  const n = Number(v ?? 0)
  if (!Number.isFinite(n)) return '0.0%'
  return `${n.toFixed(1)}%`
}

/** YYYY-MM → last calendar day as YYYY-MM-DD */
export function monthLastDay(ym: string): string {
  const [y, m] = ym.split('-').map(Number)
  const last = new Date(y, m, 0)
  const mm = String(last.getMonth() + 1).padStart(2, '0')
  const dd = String(last.getDate()).padStart(2, '0')
  return `${last.getFullYear()}-${mm}-${dd}`
}
