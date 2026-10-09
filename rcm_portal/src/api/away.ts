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
  auto_closed?: boolean
}

export type LiveStatus =
  | 'working'
  | 'idle'
  | 'locked'
  | 'unverified'
  | 'paused'
  | 'signed_out'
  | 'offline'

export type TrackerHealth = {
  extension: { last_at: string | null; state: string | null; version: string | null } | null
  tab: { last_at: string | null; permission: string | null; permission_at: string | null } | null
}

export type MyPresence = {
  status: LiveStatus
  since: string | null
  source: 'tab' | 'extension' | null
  online: boolean
  tracker: TrackerHealth
  idle_grace_seconds: number
}

export type PresencePing = {
  at: string
  source: 'tab' | 'extension'
  state: string | null
  tab_id: string | null
  visible: boolean | null
  client_at: string | null
  booked: string | null
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
  presence?: MyPresence
}

export type AwayPerson = AwayMe & {
  user_id: string
  display_name: string
  username: string
  roles: string[]
  team: string
  team_label: string
  status: 'working' | AwayKind
  online: boolean
  offline_since: string | null
  seconds_desk: number
  seconds_idle: number
  seconds_unverified?: number
  logged_in_at: string | null
  desk_permission: string | null
  live_status?: LiveStatus | null
  live_since?: string | null
  live_source?: 'tab' | 'extension' | null
  tracker?: TrackerHealth
}

export type AwayLive = {
  user_id: string
  display_name: string
  team?: string
  kind: AwayKind
  elapsed_seconds: number
  with_whom: string | null
  started_at: string
}

export type AwayDayCount = {
  work_day: string
  people: number
}

export type AwayTeam = {
  key: string
  label: string
  people: number
}

export type AwayBoard = {
  work_day: string
  is_today: boolean
  break_budget_seconds: number
  prayer_limit: number
  people: AwayPerson[]
  teams?: AwayTeam[]
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

export function endAwayFor(userId: string) {
  return api<AwayMe>(`/api/away/people/${encodeURIComponent(userId)}/end`, { method: 'POST' })
}

export function fetchPresencePings(userId: string) {
  return api<{ pings: PresencePing[] }>(`/api/away/people/${encodeURIComponent(userId)}/pings`)
}

export const LIVE_LABEL: Record<LiveStatus, string> = {
  working: 'Working',
  idle: 'Idle at desk',
  locked: 'Screen locked',
  unverified: 'Portal open, unverified',
  paused: 'Tracker paused',
  signed_out: 'Signed out',
  offline: 'Offline',
}

export function trackerNote(tracker: TrackerHealth | undefined, nowMs: number) {
  const ext = tracker?.extension
  if (ext?.last_at && nowMs - new Date(ext.last_at).getTime() < 10 * 60 * 1000) {
    return { text: ext.state === 'paused' ? 'Desk tracker paused' : 'Desk tracker on', ok: ext.state !== 'paused' }
  }
  const permission = tracker?.tab?.permission
  if (permission === 'watching') return { text: 'Browser idle detection on', ok: true }
  if (permission === 'prompt') return { text: 'Idle detection off, no desk tracker', ok: false }
  if (permission === 'denied') return { text: 'Idle detection blocked, no desk tracker', ok: false }
  if (permission === 'unsupported') return { text: 'Browser without idle detection, no desk tracker', ok: false }
  if (permission === 'granted_not_watching' || permission === 'error') {
    return { text: 'Idle detection failed to start', ok: false }
  }
  return { text: 'No tracker yet', ok: false }
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
