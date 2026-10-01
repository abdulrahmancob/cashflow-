import { useEffect, useMemo, useState } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import {
  fetchAwayBoard,
  formatClock,
  formatMinutes,
  sessionElapsed,
  type AwayBoard,
  type AwayKind,
  type AwayLive,
  type AwayPerson,
  type AwaySession,
} from '../api/away'
import { ApiError } from '../api/client'
import { Avatar } from '../components/table'
import { Badge, PageHeader } from '../components/ui'

const POLL_MS = 20_000

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

function personBucket(person: AwayPerson, isToday: boolean): 'away' | 'desk' | 'offline' {
  if (isToday && person.status !== 'working') return 'away'
  return person.online ? 'desk' : 'offline'
}

type BoardStatus = 'all' | 'away' | 'desk' | 'offline'
type BoardSort = 'status' | 'name' | 'login'

function statusRank(person: AwayPerson, isToday: boolean) {
  const bucket = personBucket(person, isToday)
  if (bucket === 'away') return 0
  if (bucket === 'desk') return 1
  return 2
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

function meetingLine(session: AwaySession) {
  const who = session.with_whom || '—'
  const planned = session.planned_seconds ? formatMinutes(session.planned_seconds) : '—'
  const taken = formatMinutes(session.elapsed_seconds)
  const state = session.ended_at ? taken : `${taken} so far`
  return `${who} · ${planned} planned · ${state}`
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
}: {
  person: AwayPerson
  isToday: boolean
  now: number
}) {
  const away = isToday && person.status !== 'working'
  const kind: AwayKind | 'working' = away ? person.status : 'working'
  const label = away ? KIND_LABEL[person.status as AwayKind] : person.online ? 'At desk' : 'Offline'
  const budget = person.break_budget_seconds || 1
  const used = Math.min(100, Math.round((person.break_seconds / budget) * 100))
  const over = person.break_seconds > person.break_budget_seconds
  const clock = person.open ? formatClock(sessionElapsed(person.open, now)) : null
  const loginTime = person.logged_in_at ? formatLoginTime(person.logged_in_at) : null
  const offlineFor =
    !away && !person.online && person.offline_since
      ? Math.max(0, Math.floor((now - new Date(person.offline_since).getTime()) / 1000))
      : null

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
          </div>
        </div>
        <div className="flex items-center gap-2">
          {clock && (
            <span className="font-display text-sm font-semibold tabular-nums text-gray-800 dark:text-gray-100">
              {clock}
            </span>
          )}
          {offlineFor != null && (
            <span className="font-display text-sm font-semibold tabular-nums text-gray-800 dark:text-gray-100">
              {formatClock(offlineFor)}
            </span>
          )}
          <Badge tone={TONE[kind]}>{label}</Badge>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
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
  const [sort, setSort] = useState<BoardSort>('status')

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
  }, [day])

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

  const selected = board?.work_day || cairoToday || ''
  const people = board?.people || []
  const isToday = !!board?.is_today
  const needle = query.trim().toLowerCase()
  const named = people.filter((person) =>
    person.display_name.toLowerCase().includes(needle),
  )
  const counts = {
    all: named.length,
    away: named.filter((person) => personBucket(person, isToday) === 'away').length,
    desk: named.filter((person) => personBucket(person, isToday) === 'desk').length,
    offline: named.filter((person) => personBucket(person, isToday) === 'offline').length,
  }
  const visible = named
    .filter((person) => status === 'all' || personBucket(person, isToday) === status)
    .slice()
    .sort(comparePeople(sort, isToday))
  const live = board?.live || []
  const liveIds = new Set(live.map((person) => person.user_id))
  const atDesk = people.filter(
    (person) => person.online && !liveIds.has(person.user_id),
  ).length

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
          <div className="text-sm text-gray-500">At their desk right now</div>
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
            ['desk', 'At desk'],
            ['offline', 'Offline'],
          ] as const
        ).map(([key, label]) => {
          const active = status === key
          return (
            <button
              key={key}
              type="button"
              onClick={() => setStatus(key)}
              className={`shrink-0 rounded-full px-3 py-1.5 text-sm font-medium transition ${
                active
                  ? 'bg-gray-900 text-white dark:bg-white dark:text-gray-900'
                  : 'bg-white text-gray-600 ring-1 ring-gray-200 hover:bg-gray-50 dark:bg-gray-900 dark:text-gray-300 dark:ring-gray-700'
              }`}
            >
              {label}
              <span className="ml-1.5 tabular-nums opacity-70">{counts[key]}</span>
            </button>
          )
        })}
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
      <div className="grid gap-3 lg:grid-cols-2">
        {visible.map((person) => (
          <PersonCard
            key={person.user_id}
            person={person}
            isToday={isToday}
            now={now}
          />
        ))}
      </div>
    </div>
  )
}
