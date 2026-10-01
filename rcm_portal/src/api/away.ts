import { api } from './client'

export type AwayKind = 'break' | 'prayer' | 'meeting'
export type AwayWarning = 'none' | 'soon' | 'over'

export type AwaySession = {
  away_id: string
  kind: AwayKind
  started_at: string
  ended_at: string | null
  planned_seconds: number | null
  with_whom: string | null
  elapsed_seconds: number
  work_day: string
}

export type AwayMe = {
  open: AwaySession | null
  break_seconds: number
  break_seconds_closed: number
  break_budget_seconds: number
  break_remaining_seconds: number
  prayer_count: number
  prayer_limit: number
  prayer_max_seconds: number
  warn_within_seconds: number
  prayer_warn_within_seconds: number
  meetings: AwaySession[]
  sessions: AwaySession[]
  warning: AwayWarning
  work_day: string
}

export type AwayPerson = AwayMe & {
  user_id: string
  display_name: string
  username: string
  roles: string[]
  status: 'working' | AwayKind
  online: boolean
  offline_since: string | null
  seconds_desk: number
  seconds_idle: number
  logged_in_at: string | null
  desk_permission: string | null
}

export type AwayLive = {
  user_id: string
  display_name: string
  kind: AwayKind
  elapsed_seconds: number
  with_whom: string | null
  started_at: string
}

export type AwayDayCount = {
  work_day: string
  people: number
}

export type AwayBoard = {
  work_day: string
  is_today: boolean
  break_budget_seconds: number
  prayer_limit: number
  people: AwayPerson[]
  live: AwayLive[]
  days: AwayDayCount[]
}

export function fetchAwayMe() {
  return api<AwayMe>('/api/away/me')
}

export function startAway(body: {
  kind: AwayKind
  planned_minutes?: number
  with_whom?: string
}) {
  return api<AwayMe>('/api/away/start', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

export function endAway() {
  return api<AwayMe>('/api/away/end', { method: 'POST' })
}

export function fetchAwayBoard(day?: string) {
  const query = day ? `?day=${encodeURIComponent(day)}` : ''
  return api<AwayBoard>(`/api/away/board${query}`)
}

export function sessionElapsed(session: AwaySession, nowMs: number) {
  const start = new Date(session.started_at).getTime()
  if (Number.isNaN(start)) return session.elapsed_seconds
  const end = session.ended_at ? new Date(session.ended_at).getTime() : nowMs
  return Math.max(0, Math.floor((end - start) / 1000))
}

export function liveWarning(state: AwayMe, nowMs: number): AwayWarning {
  const open = state.open
  if (!open) return 'none'
  const elapsed = sessionElapsed(open, nowMs)
  if (open.kind === 'break') {
    const remaining = state.break_budget_seconds - (state.break_seconds_closed + elapsed)
    if (remaining <= 0) return 'over'
    if (remaining <= state.warn_within_seconds) return 'soon'
    return 'none'
  }
  const planned = open.planned_seconds || 0
  if (planned <= 0) return 'none'
  const remaining = planned - elapsed
  if (remaining <= 0) return 'over'
  const threshold =
    open.kind === 'prayer' ? state.prayer_warn_within_seconds : state.warn_within_seconds
  if (remaining <= threshold) return 'soon'
  return 'none'
}

export function formatClock(totalSeconds: number) {
  const seconds = Math.max(0, Math.floor(totalSeconds))
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return `${minutes}:${String(rest).padStart(2, '0')}`
}

export function formatMinutes(totalSeconds: number) {
  const minutes = Math.max(0, Math.round(totalSeconds / 60))
  return `${minutes} min`
}
