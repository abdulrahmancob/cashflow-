import { api } from './client'

export type BillingMonthRow = {
  period: string
  period_start: string | null
  insurance_payment: number
  copay: number
  total_payment: number
  visits: number
  ave_visit: number
  paid_visits: number
  pending_visits: number
  denied_visits: number
  payment_pct: number
  act_ave_visit: number
  collection: Record<string, number>
  collected_visits: number
  open_visits: number
  median_days: number | null
  avg_days: number | null
  weighted_avg_days: number | null
}

export type BillingMonthly = {
  year: number
  clinic: string | null
  clinics: string[]
  rows: BillingMonthRow[]
  totals: BillingMonthRow
  aging_columns: string[]
}

export const billingAnalysisApi = {
  monthly(year: number, clinic = '') {
    const p = new URLSearchParams()
    p.set('year', String(year))
    if (clinic) p.set('clinic', clinic)
    return api<BillingMonthly>(`/api/billing-analysis/monthly?${p}`)
  },
}
