import { ApiError, api } from './client'

export type AnalyticsPreset = 'today' | 'week' | 'month' | 'custom'
export type AnalyticsGrain = 'month' | 'week' | 'day'
export type AnalyticsTeamKey =
  | 'second_submission'
  | 'eligibility'
  | 'collection'
  | 'submission'

export type TeamPerson = {
  user_id: string
  username: string
  display_name: string
  is_active: boolean
  roles: string[]
  last_login_at?: string | null
  last_activity_at?: string | null
  seconds_today: number
  seconds_week: number
  seconds_month: number
  seconds_period: number
  logins_period: number
  ss_claims: number
  ss_claims_today: number
  ss_claims_week: number
  ss_claims_month: number
  ss_money: number
  ss_paid: number
  ss_denied: number
  ss_pending: number
  ss_submitted: number
  ss_corrected: number
  ss_timely_filing: number
  ss_not_entered: number
  ss_other: number
  elig_touched: number
  elig_touched_today: number
  elig_touched_week: number
  elig_touched_month: number
  elig_completed: number
  elig_completed_today: number
  elig_completed_week: number
  elig_completed_month: number
  elig_money: number
  coll_touched: number
  coll_touched_today: number
  coll_touched_week: number
  coll_touched_month: number
  coll_worked: number
  coll_worked_today: number
  coll_worked_week: number
  coll_worked_month: number
  coll_recovered_today: number
  coll_recovered_month: number
  coll_money_today: number
  coll_money_month: number
  coll_assigned: number
  coll_finished: number
  coll_status_today: Record<string, number>
  coll_status_month: Record<string, number>
  cpt_touched: number
  cpt_touched_today: number
  cpt_touched_week: number
  cpt_touched_month: number
  cpt_resolved: number
  cpt_resolved_today: number
  cpt_resolved_week: number
  cpt_resolved_month: number
  icd_touched: number
  icd_touched_today: number
  icd_touched_week: number
  icd_touched_month: number
  icd_resolved: number
  icd_resolved_today: number
  icd_resolved_week: number
  icd_resolved_month: number
  money_total: number
}

export type TeamSummary = {
  team: AnalyticsTeamKey
  teams: Array<{ key: AnalyticsTeamKey; label: string }>
  period: { start: string; end: string }
  kpis: {
    people: number
    seconds_period: number
    ss_claims: number
    ss_claims_today: number
    ss_claims_week: number
    ss_claims_month: number
    ss_money: number
    ss_paid: number
    ss_denied: number
    ss_pending: number
    ss_submitted: number
    ss_corrected: number
    ss_timely_filing: number
    ss_not_entered: number
    elig_touched: number
    elig_touched_today: number
    elig_touched_week: number
    elig_touched_month: number
    elig_completed: number
    elig_completed_today: number
    elig_completed_week: number
    elig_completed_month: number
    elig_money: number
    coll_touched: number
    coll_touched_today: number
    coll_touched_week: number
    coll_touched_month: number
    coll_worked: number
    coll_worked_today: number
    coll_worked_week: number
    coll_worked_month: number
    coll_recovered_today: number
    coll_recovered_month: number
    coll_money_today: number
    coll_money_month: number
    coll_assigned: number
    coll_finished: number
    cpt_touched: number
    cpt_touched_today: number
    cpt_touched_week: number
    cpt_touched_month: number
    cpt_resolved: number
    cpt_resolved_today: number
    cpt_resolved_week: number
    cpt_resolved_month: number
    icd_touched: number
    icd_touched_today: number
    icd_touched_week: number
    icd_touched_month: number
    icd_resolved: number
    icd_resolved_today: number
    icd_resolved_week: number
    icd_resolved_month: number
    logins_period: number
  }
  people: TeamPerson[]
  collection_statuses?: string[]
  charts: {
    hours: Array<{ name: string; hours: number }>
    outcomes: Array<{
      name: string
      paid: number
      denied: number
      timely_filing: number
    }>
  }
}

export type TeamUserDetail = {
  team?: AnalyticsTeamKey
  user: {
    user_id: string
    display_name: string
    username: string
    roles: string[]
    last_login_at?: string | null
  }
  hours: Array<{ day: string; seconds: number }>
  second_submission: Array<Record<string, string | null>>
  eligibility?: Array<Record<string, string | null>>
  collection?: Array<Record<string, string | null>>
  cpt_audit?: Array<Record<string, string | null>>
}

export type SsBreakdownRow = {
  period: string
  period_start: string | null
  payment: number
  claims: number
  total_submitted_claims: number
  submitted: number
  paid: number
  pending: number
  denied: number
  corrected: number
  timely_filing: number
}

export type CollectionRootCauseRow = {
  period: string
  period_start: string
  counts: Record<string, number>
  top: string
  top_count: number
}

export type CollectionRootCausePerson = {
  user_id: string
  display_name: string
  counts: Record<string, number>
  top: string
  top_count: number
}

export type DeadRootCauseRow = {
  label: string
  count: number
  percent: number
}

export type DeadRootCauseBreakdown = {
  total: number
  rows: DeadRootCauseRow[]
}

export type CollectionRootCauseBreakdown = {
  year: number
  labels: string[]
  rows: CollectionRootCauseRow[]
  people: CollectionRootCausePerson[]
}

export type SsBreakdown = {
  grain: AnalyticsGrain
  year: number
  month: number | null
  rows: SsBreakdownRow[]
  totals: SsBreakdownRow
  member: { user_id: string; display_name: string } | null
  people: Array<{ user_id: string; display_name: string }>
}

function qs(preset: AnalyticsPreset, dateFrom: string, dateTo: string, team?: string) {
  const p = new URLSearchParams()
  p.set('preset', preset)
  if (team) p.set('team', team)
  if (preset === 'custom') {
    if (dateFrom) p.set('date_from', dateFrom)
    if (dateTo) p.set('date_to', dateTo)
  }
  return p.toString()
}

export const analyticsApi = {
  team(preset: AnalyticsPreset, dateFrom = '', dateTo = '', team = 'second_submission') {
    return api<TeamSummary>(`/api/analytics/team?${qs(preset, dateFrom, dateTo, team)}`)
  },
  user(
    userId: string,
    preset: AnalyticsPreset,
    dateFrom = '',
    dateTo = '',
    team = 'second_submission',
  ) {
    return api<TeamUserDetail>(
      `/api/analytics/team/${encodeURIComponent(userId)}?${qs(preset, dateFrom, dateTo, team)}`,
    )
  },
  myAssignments() {
    return api<{ assigned: number; finished: number }>('/api/analytics/my-assignments')
  },
  collectionDeadRootCauses() {
    return api<DeadRootCauseBreakdown>('/api/analytics/collection/dead-root-causes')
  },
  collectionRootCauses(year?: number) {
    const p = new URLSearchParams()
    if (year) p.set('year', String(year))
    const q = p.toString()
    return api<CollectionRootCauseBreakdown>(
      `/api/analytics/collection/root-causes${q ? `?${q}` : ''}`,
    )
  },
  ssBreakdown(opts: {
    grain?: AnalyticsGrain
    year?: number
    month?: string
    userId?: string
  } = {}) {
    const p = new URLSearchParams()
    p.set('grain', opts.grain || 'month')
    if (opts.year) p.set('year', String(opts.year))
    if (opts.month) p.set('month', opts.month)
    if (opts.userId) p.set('user_id', opts.userId)
    return api<SsBreakdown>(`/api/analytics/ss/breakdown?${p.toString()}`)
  },
}

const EXPORT_FILENAMES: Record<AnalyticsTeamKey, string> = {
  second_submission: 'ss-team-analytics.xlsx',
  eligibility: 'eligibility-team-analytics.xlsx',
  collection: 'collection-team-analytics.xlsx',
  submission: 'submission-team-analytics.xlsx',
}

export async function downloadAnalyticsSheet(opts: {
  team: AnalyticsTeamKey
  preset: AnalyticsPreset
  dateFrom?: string
  dateTo?: string
  year?: number
  grain?: AnalyticsGrain
  month?: string
  userId?: string
}) {
  const p = new URLSearchParams()
  p.set('team', opts.team)
  p.set('preset', opts.preset)
  if (opts.preset === 'custom') {
    if (opts.dateFrom) p.set('date_from', opts.dateFrom)
    if (opts.dateTo) p.set('date_to', opts.dateTo)
  }
  if (opts.year) p.set('year', String(opts.year))
  p.set('grain', opts.grain || 'month')
  if (opts.month) p.set('month', opts.month)
  if (opts.userId) p.set('user_id', opts.userId)
  const res = await fetch(`/api/analytics/export?${p.toString()}`, {
    credentials: 'include',
  })
  if (!res.ok) {
    let detail: unknown = res.statusText
    try {
      detail = (await res.json()).detail
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail)
  }
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = EXPORT_FILENAMES[opts.team]
  a.click()
  URL.revokeObjectURL(url)
}
