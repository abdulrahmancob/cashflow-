import { useEffect, useMemo, useState } from 'react'
import { ChevronLeft, ChevronRight, Download } from 'lucide-react'
import {
  endAwayFor,
  fetchAwayBoard,
  fetchPresencePings,
  formatClock,
  formatMinutes,
  LIVE_LABEL,
  sessionElapsed,
  trackerNote,
  type AwayBoard,
  type AwayKind,
  type AwayLive,
  type AwayPerson,
  type AwaySession,
  type LiveStatus,
  type PresencePing,
} from '../api/away'
import { ApiError } from '../api/client'
import { Avatar } from '../components/table'
import { Badge, Button, Drawer, PageHeader } from '../components/ui'

const POLL_MS = 20_000
const TEAM_KEY = 'awayBoard.team'

function readStoredTeam() {
  try {
    return window.localStorage.getItem(TEAM_KEY) || 'all'
  } catch {
    return 'all'
  }
}

function storeTeam(team: string) {
  try {
    window.localStorage.setItem(TEAM_KEY, team)
  } catch {
    // Private windows can block storage; the choice just will not stick.
  }
}

const KIND_LABEL: Record<AwayKind, string> = {
  break: 'Break',
  prayer: 'Prayer',
  meeting: 'Meeting',
}

const RAIL: Record<AwayKind | 'working', string> = {
  working: 'border-l-gray-200 dark:border-l-gray-700',
  break: 'border-l-amber-400',
  prayer: 'border-l-sky-500',
  meeting: 'border-l-violet-500',
}

const TONE: Record<AwayKind | 'working', 'gray' | 'amber' | 'blue' | 'purple'> = {
  working: 'gray',
  break: 'amber',
  prayer: 'blue',
  meeting: 'purple',
}

function shiftDay(iso: string, delta: number) {
  const [year, month, day] = iso.split('-').map(Number)
  const next = new Date(Date.UTC(year, month - 1, day + delta))
  return next.toISOString().slice(0, 10)
}

function formatDayLabel(iso: string) {
  const [year, month, day] = iso.split('-').map(Number)
  return new Date(Date.UTC(year, month - 1, day)).toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  })
}

function formatDayTitle(iso: string) {
  const [year, month, day] = iso.split('-').map(Number)
  return new Date(Date.UTC(year, month - 1, day)).toLocaleDateString('en-US', {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
    timeZone: 'UTC',
  })
}

function formatDuration(totalSeconds: number) {
  const seconds = Math.max(0, Math.round(totalSeconds || 0))
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  if (hours === 0) return `${minutes}m`
  if (minutes === 0) return `${hours}h`
  return `${hours}h ${minutes}m`
}

function formatLoginTime(iso: string) {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return null
  return date.toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    timeZone: 'Africa/Cairo',
  })
}

const LIVE_TONE: Record<LiveStatus, 'gray' | 'amber' | 'blue' | 'green'> = {
  working: 'green',
  idle: 'amber',
  locked: 'amber',
  unverified: 'blue',
  paused: 'gray',
  signed_out: 'gray',
  offline: 'gray',
}

type Bucket = 'away' | 'working' | 'idle' | 'unverified' | 'offline'
type BoardStatus = 'all' | Bucket
type BoardSort = 'status' | 'name' | 'login'

const BUCKET_RANK: Record<Bucket, number> = {
  away: 0,
  working: 1,
  idle: 2,
  unverified: 3,
  offline: 4,
}

function personBucket(person: AwayPerson, isToday: boolean): Bucket {
  if (isToday && person.status !== 'working') return 'away'
  const live = person.live_status
  if (!live) return person.online ? 'working' : 'offline'
  if (live === 'working') return 'working'
  if (live === 'idle' || live === 'locked') return 'idle'
  if (live === 'unverified' || live === 'paused') return 'unverified'
  return 'offline'
}

function statusRank(person: AwayPerson, isToday: boolean) {
  return BUCKET_RANK[personBucket(person, isToday)]
}

function secondsSince(iso: string | null | undefined, now: number) {
  if (!iso) return null
  const time = new Date(iso).getTime()
  if (Number.isNaN(time)) return null
  return Math.max(0, Math.floor((now - time) / 1000))
}

function loginRank(person: AwayPerson) {
  if (!person.logged_in_at) return Number.POSITIVE_INFINITY
  const time = new Date(person.logged_in_at).getTime()
  return Number.isNaN(time) ? Number.POSITIVE_INFINITY : time
}

function comparePeople(sort: BoardSort, isToday: boolean) {
  return (a: AwayPerson, b: AwayPerson) => {
    const byName = a.display_name.localeCompare(b.display_name, undefined, { sensitivity: 'base' })
    if (sort === 'name') return byName
    if (sort === 'login') {
      const aKey = loginRank(a)
      const bKey = loginRank(b)
      if (aKey !== bKey) return aKey < bKey ? -1 : 1
      return byName
    }
    const delta = statusRank(a, isToday) - statusRank(b, isToday)
    return delta === 0 ? byName : delta
  }
}

function FilterChip({
  active,
  label,
  count,
  onClick,
}: {
  active: boolean
  label: string
  count: number
  onClick: () => void
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={`shrink-0 rounded-full px-3 py-1.5 text-sm font-medium transition ${
        active
          ? 'bg-gray-900 text-white dark:bg-white dark:text-gray-900'
          : 'bg-white text-gray-600 ring-1 ring-gray-200 hover:bg-gray-50 dark:bg-gray-900 dark:text-gray-300 dark:ring-gray-700'
      }`}
    >
      {label}
      <span className="ml-1.5 tabular-nums opacity-70">{count}</span>
    </button>
  )
}

function meetingLine(session: AwaySession) {
  const who = session.with_whom || '—'
  const planned = session.planned_seconds ? formatMinutes(session.planned_seconds) : '—'
  const taken = formatMinutes(session.elapsed_seconds)
  const state = session.ended_at ? taken : `${taken} so far`
  return `${who} · ${planned} planned · ${state}`
}

function formatPingTime(iso: string) {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
    timeZone: 'Africa/Cairo',
  })
}

function PingLog({ pings }: { pings: PresencePing[] }) {
  if (pings.length === 0) {
    return <p className="text-sm text-gray-500">No signal from this person in the last 48 hours.</p>
  }
  return (
    <table className="w-full text-left text-sm">
      <thead className="text-[11px] uppercase tracking-wider text-gray-500">
        <tr>
          <th className="py-1 pr-3">Time</th>
          <th className="py-1 pr-3">From</th>
          <th className="py-1 pr-3">State</th>
          <th className="py-1 pr-3">Tab</th>
          <th className="py-1">Booked</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
        {pings.map((ping, index) => (
          <tr key={`${ping.at}-${index}`} className="text-gray-700 dark:text-gray-200">
            <td className="py-1 pr-3 tabular-nums">{formatPingTime(ping.at)}</td>
            <td className="py-1 pr-3">{ping.source === 'extension' ? 'Desk tracker' : 'Portal tab'}</td>
            <td className="py-1 pr-3">{ping.state || '—'}</td>
            <td className="py-1 pr-3">
              {ping.source === 'tab' ? (ping.visible ? 'Visible' : 'Hidden') : '—'}
            </td>
            <td className="py-1 tabular-nums">{ping.booked || '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function AwayNowCard({ live, now }: { live: AwayLive[]; now: number }) {
  const [hover, setHover] = useState(false)
  const [pinned, setPinned] = useState(false)
  const shown = hover || pinned
  const busy = live.length > 0

  return (
    <div
      className="relative"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <button
        type="button"
        aria-expanded={shown}
        onClick={() => setPinned((value) => !value)}
        className={`w-full rounded-2xl border px-5 py-4 text-left shadow-xs transition ${
          busy
            ? 'border-rose-200 bg-rose-50 dark:border-rose-900/70 dark:bg-rose-950/40'
            : 'border-emerald-200 bg-emerald-50 dark:border-emerald-900/60 dark:bg-emerald-950/30'
        }`}
      >
        <div className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">
          Away now
        </div>
        <div className="mt-1 flex items-end justify-between gap-3">
          <div
            className={`font-display text-4xl font-semibold tabular-nums ${
              busy ? 'text-rose-700 dark:text-rose-200' : 'text-emerald-800 dark:text-emerald-100'
            }`}
          >
            {live.length}
          </div>
          <div className="pb-1 text-sm text-gray-600 dark:text-gray-300">
            {busy ? 'Hover to see who' : 'Everyone is at their desk'}
          </div>
        </div>
      </button>
      {shown && (
        <div className="absolute left-0 right-0 top-full z-30 mt-2 rounded-2xl border border-gray-200 bg-white p-2 shadow-xl dark:border-gray-700 dark:bg-gray-900">
          {live.length === 0 ? (
            <p className="px-3 py-4 text-sm text-gray-500">Everyone is at their desk.</p>
          ) : (
            <ul className="max-h-80 space-y-1 overflow-y-auto">
              {live.map((person) => {
                const elapsed = sessionElapsed(
                  {
                    away_id: person.user_id,
                    kind: person.kind,
                    started_at: person.started_at,
                    ended_at: null,
                    planned_seconds: null,
                    with_whom: person.with_whom,
                    elapsed_seconds: person.elapsed_seconds,
                    work_day: '',
                  },
                  now,
                )
                return (
                  <li
                    key={person.user_id}
                    className="flex items-center gap-3 rounded-xl px-2 py-2 hover:bg-gray-50 dark:hover:bg-gray-800"
                  >
                    <Avatar name={person.display_name} size="sm" />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-semibold text-gray-900 dark:text-white">
                        {person.display_name}
                      </div>
                      <div className="truncate text-xs text-gray-500">
                        {KIND_LABEL[person.kind]}
                        {person.kind === 'meeting' && person.with_whom
                          ? ` with ${person.with_whom}`
                          : ''}
                      </div>
                    </div>
                    <span className="font-display text-sm font-semibold tabular-nums text-gray-800 dark:text-gray-100">
                      {formatClock(elapsed)}
                    </span>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

function PersonCard({
  person,
  isToday,
  now,
  onEnd,
  onDiagnose,
}: {
  person: AwayPerson
  isToday: boolean
  now: number
  onEnd: (person: AwayPerson) => void
  onDiagnose: (person: AwayPerson) => void
}) {
  const away = isToday && person.status !== 'working'
  const kind: AwayKind | 'working' = away ? person.status : 'working'
  const live = isToday ? person.live_status ?? null : null
  const label = away
    ? KIND_LABEL[person.status as AwayKind]
    : live
      ? LIVE_LABEL[live]
      : person.online
        ? 'At desk'
        : isToday
          ? 'Offline'
          : 'Day total'
  const tone = away ? TONE[kind] : live ? LIVE_TONE[live] : TONE.working
  const budget = person.break_budget_seconds || 1
  const used = Math.min(100, Math.round((person.break_seconds / budget) * 100))
  const over = person.break_seconds > person.break_budget_seconds
  const clock = person.open ? formatClock(sessionElapsed(person.open, now)) : null
  const loginTime = person.logged_in_at ? formatLoginTime(person.logged_in_at) : null
  const tracker = isToday ? trackerNote(person.tracker, now) : null
  const sinceFor =
    !away && live && live !== 'working'
      ? secondsSince(person.live_since, now)
      : !away && !live && !person.online
        ? secondsSince(person.offline_since, now)
        : null
  const autoClosed = (person.sessions || []).some((session) => session.auto_closed)

  return (
    <article
      className={`rounded-2xl border border-gray-200 border-l-4 bg-white p-4 shadow-xs dark:border-gray-800 dark:bg-gray-900 ${RAIL[kind]}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <Avatar name={person.display_name} />
          <div className="min-w-0">
            <div className="truncate font-display text-base font-semibold text-gray-900 dark:text-white">
              {person.display_name}
            </div>
            <div className="truncate text-xs text-gray-500">
              {person.roles.join(', ') || person.username}
            </div>
            <div className="truncate text-xs text-gray-500">
              {loginTime ? `Logged in ${loginTime}` : 'No login'}
            </div>
            {tracker && (
              <div
                className={`truncate text-xs font-medium ${
                  tracker.ok
                    ? 'text-emerald-700 dark:text-emerald-300'
                    : 'text-rose-600 dark:text-rose-300'
                }`}
              >
                {tracker.text}
              </div>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {clock && (
            <span className="font-display text-sm font-semibold tabular-nums text-gray-800 dark:text-gray-100">
              {clock}
            </span>
          )}
          {sinceFor != null && (
            <span className="font-display text-sm font-semibold tabular-nums text-gray-800 dark:text-gray-100">
              {formatClock(sinceFor)}
            </span>
          )}
          <Badge tone={tone}>{label}</Badge>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2 text-sm">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">At computer</div>
          <div className="font-display text-lg font-semibold tabular-nums text-gray-900 dark:text-white">
            {formatDuration(person.seconds_desk)}
          </div>
        </div>
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">Idle</div>
          <div className="font-display text-lg font-semibold tabular-nums text-gray-900 dark:text-white">
            {formatDuration(person.seconds_idle)}
          </div>
        </div>
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">Unverified</div>
          <div className="font-display text-lg font-semibold tabular-nums text-gray-900 dark:text-white">
            {formatDuration(person.seconds_unverified || 0)}
          </div>
        </div>
      </div>
      <div className="mt-4">
        <div className="mb-1 flex items-center justify-between text-xs text-gray-500">
          <span>Break</span>
          <span className={`tabular-nums ${over ? 'font-semibold text-rose-600' : ''}`}>
            {formatMinutes(person.break_seconds)} / {formatMinutes(person.break_budget_seconds)}
          </span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
          <div
            className={`h-full rounded-full ${over ? 'bg-rose-500' : 'bg-amber-400'}`}
            style={{ width: `${used}%` }}
          />
        </div>
      </div>
      <div className="mt-3 flex items-center justify-between gap-3">
        <div className="text-xs text-gray-500">Prayers</div>
        <div className="flex gap-1.5" aria-label={`${person.prayer_count} of ${person.prayer_limit} prayers`}>
          {Array.from({ length: person.prayer_limit }, (_, index) => (
            <span
              key={index}
              className={`h-2.5 w-2.5 rounded-full ${
                index < person.prayer_count
                  ? 'bg-sky-500'
                  : 'bg-gray-200 dark:bg-gray-700'
              }`}
            />
          ))}
        </div>
      </div>
      {person.meetings.length > 0 && (
        <ul className="mt-3 space-y-1 border-t border-gray-100 pt-3 text-sm text-gray-600 dark:border-gray-800 dark:text-gray-300">
          {person.meetings.map((session) => (
            <li key={session.away_id}>{meetingLine(session)}</li>
          ))}
        </ul>
      )}
      {autoClosed && (
        <p className="mt-3 text-xs font-medium text-amber-700 dark:text-amber-300">
          A forgotten break or meeting was closed automatically at its limit.
        </p>
      )}
      {isToday && (
        <div className="mt-3 flex flex-wrap justify-end gap-2 border-t border-gray-100 pt-3 dark:border-gray-800">
          {person.open && (
            <Button variant="secondary" size="sm" type="button" onClick={() => onEnd(person)}>
              End {KIND_LABEL[person.open.kind].toLowerCase()}
            </Button>
          )}
          <Button variant="ghost" size="sm" type="button" onClick={() => onDiagnose(person)}>
            Why this status?
          </Button>
        </div>
      )}
    </article>
  )
}

export function AwayBoardPage() {
  const [board, setBoard] = useState<AwayBoard | null>(null)
  const [today, setToday] = useState<string | null>(null)
  const [day, setDay] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<BoardStatus>('all')
  const [team, setTeam] = useState<string>(readStoredTeam)
  const [sort, setSort] = useState<BoardSort>('status')
  const [exporting, setExporting] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const [diagnose, setDiagnose] = useState<AwayPerson | null>(null)
  const [pings, setPings] = useState<PresencePing[] | null>(null)

  useEffect(() => {
    const tick = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(tick)
  }, [])

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const row = await fetchAwayBoard(day ?? undefined)
        if (cancelled) return
        setBoard(row)
        setError(null)
        if (row.is_today) setToday(row.work_day)
      } catch (e) {
        if (!cancelled) setError(e instanceof ApiError ? e.message : 'Could not load the board')
      }
    }
    void load()
    if (day !== null) {
      return () => {
        cancelled = true
      }
    }
    const poll = window.setInterval(() => void load(), POLL_MS)
    return () => {
      cancelled = true
      window.clearInterval(poll)
    }
  }, [day, refresh])

  useEffect(() => {
    if (!diagnose) return
    let cancelled = false
    setPings(null)
    fetchPresencePings(diagnose.user_id)
      .then((row) => {
        if (!cancelled) setPings(row.pings)
      })
      .catch(() => {
        if (!cancelled) setPings([])
      })
    return () => {
      cancelled = true
    }
  }, [diagnose])

  function endFor(person: AwayPerson) {
    const what = person.open ? KIND_LABEL[person.open.kind].toLowerCase() : 'session'
    if (!window.confirm(`End ${person.display_name}'s ${what} now?`)) return
    endAwayFor(person.user_id)
      .then(() => setRefresh((value) => value + 1))
      .catch((e) => setError(e instanceof ApiError ? e.message : 'Could not end it.'))
  }

  const cairoToday = today
  const chips = useMemo(() => {
    if (!cairoToday) return []
    const yesterday = shiftDay(cairoToday, -1)
    const recorded = (board?.days || []).map((item) => item.work_day)
    const ordered = [cairoToday, yesterday, ...recorded]
    return [...new Set(ordered)]
  }, [board?.days, cairoToday])

  function choose(next: string | null) {
    if (!next || (cairoToday && next === cairoToday)) {
      setDay(null)
      return
    }
    setDay(next)
  }

  function chooseTeam(next: string) {
    setTeam(next)
    storeTeam(next)
  }

  function downloadExcel() {
    setExporting(true)
    setError(null)
    const query = activeTeam === 'all' ? '' : `?team=${encodeURIComponent(activeTeam)}`
    void fetch(`/api/away/board/export${query}`, { credentials: 'include' })
      .then(async (response) => {
        if (!response.ok) throw new Error('export failed')
        return response.blob()
      })
      .then((blob) => {
        const url = URL.createObjectURL(blob)
        const link = document.createElement('a')
        link.href = url
        link.download = activeTeam === 'all' ? 'away_board.xlsx' : `away_board_${activeTeam}.xlsx`
        link.click()
        URL.revokeObjectURL(url)
      })
      .catch(() => setError('Could not download the sheet.'))
      .finally(() => setExporting(false))
  }

  const selected = board?.work_day || cairoToday || ''
  const allPeople = board?.people || []
  const teams = board?.teams || []
  const activeTeam = teams.some((item) => item.key === team) ? team : 'all'
  const people =
    activeTeam === 'all' ? allPeople : allPeople.filter((person) => person.team === activeTeam)
  const isToday = !!board?.is_today
  const needle = query.trim().toLowerCase()
  const named = people.filter((person) =>
    person.display_name.toLowerCase().includes(needle),
  )
  const counts: Record<BoardStatus, number> = {
    all: named.length,
    away: 0,
    working: 0,
    idle: 0,
    unverified: 0,
    offline: 0,
  }
  for (const person of named) counts[personBucket(person, isToday)] += 1
  const visible = named
    .filter((person) => status === 'all' || personBucket(person, isToday) === status)
    .slice()
    .sort(comparePeople(sort, isToday))
  const live = (board?.live || []).filter(
    (row) => activeTeam === 'all' || row.team === activeTeam,
  )
  const atDesk = people.filter((person) => personBucket(person, true) === 'working').length
  const grouped = activeTeam === 'all' && teams.length > 1
  const sections = grouped
    ? teams
        .map((item) => ({
          key: item.key,
          label: item.label,
          members: visible.filter((person) => person.team === item.key),
        }))
        .filter((section) => section.members.length > 0)
    : [{ key: activeTeam, label: '', members: visible }]

  return (
    <div className="space-y-5">
      <PageHeader
        title="Away board"
        description={
          board && !board.is_today
            ? `Viewing ${formatDayTitle(board.work_day)}. Away now stays live.`
            : 'Who is out right now, and what each day looked like.'
        }
      />
      {error && <p className="text-sm text-rose-600">{error}</p>}
      {teams.length > 1 && (
        <div className="flex max-w-full gap-1.5 overflow-x-auto pb-1" aria-label="Teams">
          <FilterChip
            active={activeTeam === 'all'}
            label="All teams"
            count={allPeople.length}
            onClick={() => chooseTeam('all')}
          />
          {teams.map((item) => (
            <FilterChip
              key={item.key}
              active={activeTeam === item.key}
              label={item.label}
              count={item.people}
              onClick={() => chooseTeam(item.key)}
            />
          ))}
        </div>
      )}
      <div className="grid gap-3 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <AwayNowCard live={live} now={now} />
        </div>
        <div className="rounded-2xl border border-gray-200 bg-white px-5 py-4 shadow-xs dark:border-gray-800 dark:bg-gray-900">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">
            Working
          </div>
          <div className="mt-1 font-display text-4xl font-semibold tabular-nums text-gray-900 dark:text-white">
            {atDesk}
          </div>
          <div className="text-sm text-gray-500">Active at their computer right now</div>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          aria-label="Previous day"
          className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-gray-200 bg-white text-gray-600 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300"
          onClick={() => selected && choose(shiftDay(selected, -1))}
        >
          <ChevronLeft className="h-4 w-4" />
        </button>
        <div className="flex max-w-full gap-1.5 overflow-x-auto">
          {chips.map((iso) => {
            const active = iso === (day ?? cairoToday)
            const count = board?.days.find((item) => item.work_day === iso)?.people
            const label = iso === cairoToday ? 'Today' : iso === (cairoToday && shiftDay(cairoToday, -1)) ? 'Yesterday' : formatDayLabel(iso)
            return (
              <button
                key={iso}
                type="button"
                onClick={() => choose(iso)}
                className={`shrink-0 rounded-full px-3 py-1.5 text-sm font-medium transition ${
                  active
                    ? 'bg-gray-900 text-white dark:bg-white dark:text-gray-900'
                    : 'bg-white text-gray-600 ring-1 ring-gray-200 hover:bg-gray-50 dark:bg-gray-900 dark:text-gray-300 dark:ring-gray-700'
                }`}
              >
                {label}
                {count ? <span className="ml-1.5 tabular-nums opacity-70">{count}</span> : null}
              </button>
            )
          })}
        </div>
        <button
          type="button"
          aria-label="Next day"
          className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-gray-200 bg-white text-gray-600 hover:bg-gray-50 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300"
          onClick={() => selected && choose(shiftDay(selected, 1))}
        >
          <ChevronRight className="h-4 w-4" />
        </button>
        <input
          type="date"
          aria-label="Filter by date"
          value={selected}
          onChange={(event) => choose(event.target.value || null)}
          className="h-9 rounded-lg border border-gray-200 bg-white px-2 text-sm text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100"
        />
        <Button
          variant="secondary"
          size="sm"
          type="button"
          disabled={exporting}
          onClick={downloadExcel}
        >
          <Download className="h-4 w-4" />
          {exporting ? 'Downloading…' : 'Download Excel'}
        </Button>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="search"
          aria-label="Search people"
          placeholder="Search people"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          className="h-9 w-56 rounded-lg border border-gray-200 bg-white px-3 text-sm text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100"
        />
        {(
          [
            ['all', 'All'],
            ['away', 'Away'],
            ['working', 'Working'],
            ['idle', 'Idle'],
            ['unverified', 'Unverified'],
            ['offline', 'Offline'],
          ] as const
        ).map(([key, label]) => (
          <FilterChip
            key={key}
            active={status === key}
            label={label}
            count={counts[key]}
            onClick={() => setStatus(key)}
          />
        ))}
        <select
          aria-label="Sort people"
          value={sort}
          onChange={(event) => setSort(event.target.value as BoardSort)}
          className="h-9 rounded-lg border border-gray-200 bg-white px-2 text-sm text-gray-800 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-100"
        >
          <option value="status">Status</option>
          <option value="name">Name</option>
          <option value="login">Login</option>
        </select>
      </div>
      {board && !board.is_today && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-950 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-100">
          <span>This is {formatDayTitle(board.work_day)}, not the live floor.</span>
          <button type="button" className="font-semibold underline" onClick={() => choose(null)}>
            Back to today
          </button>
        </div>
      )}
      {board && visible.length === 0 && (
        <p className="text-sm text-gray-500">No one matches.</p>
      )}
      {sections.map((section) => (
        <section key={section.key} className="space-y-2">
          {grouped && (
            <h2 className="flex items-baseline gap-2 border-b border-gray-200 pb-1 text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:border-gray-800">
              {section.label}
              <span className="tabular-nums opacity-70">{section.members.length}</span>
            </h2>
          )}
          <div className="grid gap-3 lg:grid-cols-2">
            {section.members.map((person) => (
              <PersonCard
                key={person.user_id}
                person={person}
                isToday={isToday}
                now={now}
                onEnd={endFor}
                onDiagnose={setDiagnose}
              />
            ))}
          </div>
        </section>
      ))}
      <Drawer
        open={!!diagnose}
        onClose={() => setDiagnose(null)}
        title={diagnose ? `${diagnose.display_name}: last signals` : ''}
      >
        <p className="mb-3 text-sm text-gray-600 dark:text-gray-300">
          Every signal the board received, newest first. A gap of more than two minutes is no
          signal: the tab was closed or frozen, the computer slept, or the network dropped. It is
          not counted as idle.
        </p>
        {pings ? <PingLog pings={pings} /> : <p className="text-sm text-gray-500">Loading…</p>}
      </Drawer>
    </div>
  )
}
