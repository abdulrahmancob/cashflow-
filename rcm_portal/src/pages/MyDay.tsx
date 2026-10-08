import { useState } from 'react'
import {
  formatClock,
  formatMinutes,
  sessionElapsed,
  startAway,
  endAway,
  type AwayKind,
  type AwaySession,
} from '../api/away'
import { useAway } from '../components/AwayControl'
import { BoardPresenceCard } from '../components/BoardPresenceCard'
import { DeskTrackerConnect } from '../components/DeskTrackerConnect'
import { Badge, Button, Field, Input, PageHeader } from '../components/ui'

const KIND_LABEL: Record<AwayKind, string> = {
  break: 'Break',
  prayer: 'Prayer',
  meeting: 'Meeting',
}

const TONE: Record<AwayKind | 'desk', 'gray' | 'amber' | 'blue' | 'purple'> = {
  desk: 'gray',
  break: 'amber',
  prayer: 'blue',
  meeting: 'purple',
}

function formatWhen(iso: string) {
  const value = new Date(iso)
  if (Number.isNaN(value.getTime())) return '—'
  return value.toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    timeZone: 'Africa/Cairo',
  })
}

function sessionRange(session: AwaySession, now: number) {
  const start = formatWhen(session.started_at)
  if (!session.ended_at) {
    return `${start} – now · ${formatClock(sessionElapsed(session, now))} so far`
  }
  return `${start} – ${formatWhen(session.ended_at)} · ${formatMinutes(sessionElapsed(session, now))}`
}

export function MyDayPage() {
  const { me, now, error, busy, warning, run, setError } = useAway()
  const [meetingMinutes, setMeetingMinutes] = useState('30')
  const [withWhom, setWithWhom] = useState('')

  if (!me) {
    return (
      <div className="flex min-h-64 items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-gray-200 border-t-brand-600" />
      </div>
    )
  }

  async function begin(kind: AwayKind) {
    const body: { kind: AwayKind; planned_minutes?: number; with_whom?: string } = { kind }
    if (kind === 'prayer') body.planned_minutes = 10
    if (kind === 'meeting') {
      const minutes = Number(meetingMinutes)
      if (!Number.isFinite(minutes) || minutes < 1) {
        setError('Enter how long the meeting will take')
        return
      }
      if (!withWhom.trim()) {
        setError('Write who the meeting is with')
        return
      }
      body.planned_minutes = Math.round(minutes)
      body.with_whom = withWhom.trim()
    }
    const ok = await run(() => startAway(body))
    if (ok && kind === 'meeting') setWithWhom('')
  }

  const open = me.open
  const status: AwayKind | 'desk' = open ? open.kind : 'desk'
  const label = open ? KIND_LABEL[open.kind] : 'At desk'
  const clock = open ? formatClock(sessionElapsed(open, now)) : null
  const over = warning === 'over'
  const budget = me.break_budget_seconds || 1
  const used = Math.min(100, Math.round((me.break_seconds / budget) * 100))
  const breakOver = me.break_seconds > me.break_budget_seconds
  const prayersLeft = Math.max(0, me.prayer_limit - me.prayer_count)
  const sessions = [...me.sessions].sort((a, b) => a.started_at.localeCompare(b.started_at))

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <PageHeader
        title="My day"
        description="Log a break, prayer, or meeting, and see what you already took today."
      />
      {error && <p className="text-sm text-rose-600">{error}</p>}

      <section
        className={`rounded-2xl border px-5 py-5 shadow-xs ${
          over
            ? 'border-rose-300 bg-rose-50 dark:border-rose-800 dark:bg-rose-950/40'
            : 'border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900'
        }`}
      >
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <div className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">
              Right now
            </div>
            <div className="mt-2 flex items-center gap-3">
              <Badge tone={TONE[status]}>{label}</Badge>
              {clock && (
                <span className="font-display text-3xl font-semibold tabular-nums text-gray-900 dark:text-white">
                  {clock}
                </span>
              )}
            </div>
            {open?.kind === 'meeting' && open.with_whom && (
              <p className="mt-2 text-sm text-gray-600 dark:text-gray-300">With {open.with_whom}</p>
            )}
          </div>
          {open && (
            <Button
              type="button"
              variant={over ? 'danger' : 'primary'}
              disabled={busy}
              onClick={() => void run(() => endAway())}
            >
              I'm back
            </Button>
          )}
        </div>
      </section>

      <BoardPresenceCard presence={me.presence} now={now}>
        <DeskTrackerConnect />
      </BoardPresenceCard>

      {!open && (
        <div className="grid gap-3 md:grid-cols-3">
          <section className="rounded-2xl border border-gray-200 bg-white p-4 shadow-xs dark:border-gray-800 dark:bg-gray-900">
            <div className="text-sm font-semibold text-gray-900 dark:text-white">Break</div>
            <p className="mt-1 text-xs text-gray-500">
              {formatMinutes(me.break_remaining_seconds)} left of {formatMinutes(me.break_budget_seconds)} today.
            </p>
            <Button
              type="button"
              className="mt-3"
              disabled={busy}
              onClick={() => void begin('break')}
            >
              Start break
            </Button>
          </section>
          <section className="rounded-2xl border border-gray-200 bg-white p-4 shadow-xs dark:border-gray-800 dark:bg-gray-900">
            <div className="text-sm font-semibold text-gray-900 dark:text-white">Prayer</div>
            <p className="mt-1 text-xs text-gray-500">
              {me.prayer_count} of {me.prayer_limit} today. Each one is 10 minutes.
            </p>
            <Button
              type="button"
              className="mt-3"
              disabled={busy || prayersLeft === 0}
              onClick={() => void begin('prayer')}
            >
              {prayersLeft === 0 ? 'No prayers left' : 'Start prayer'}
            </Button>
          </section>
          <section className="rounded-2xl border border-gray-200 bg-white p-4 shadow-xs dark:border-gray-800 dark:bg-gray-900">
            <div className="text-sm font-semibold text-gray-900 dark:text-white">Meeting</div>
            <div className="mt-2 space-y-2">
              <Field label="Minutes">
                <Input
                  type="number"
                  min={1}
                  value={meetingMinutes}
                  onChange={(event) => setMeetingMinutes(event.target.value)}
                />
              </Field>
              <Field label="With">
                <Input
                  value={withWhom}
                  placeholder="Name"
                  onChange={(event) => setWithWhom(event.target.value)}
                />
              </Field>
            </div>
            <Button
              type="button"
              className="mt-3"
              disabled={busy}
              onClick={() => void begin('meeting')}
            >
              Start meeting
            </Button>
          </section>
        </div>
      )}

      <section className="rounded-2xl border border-gray-200 bg-white p-5 shadow-xs dark:border-gray-800 dark:bg-gray-900">
        <div className="mb-1 flex items-center justify-between text-xs text-gray-500">
          <span>Break</span>
          <span className={`tabular-nums ${breakOver ? 'font-semibold text-rose-600' : ''}`}>
            {formatMinutes(me.break_seconds)} / {formatMinutes(me.break_budget_seconds)}
          </span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
          <div
            className={`h-full rounded-full ${breakOver ? 'bg-rose-500' : 'bg-amber-400'}`}
            style={{ width: `${used}%` }}
          />
        </div>
        <div className="mt-4 flex items-center justify-between gap-3">
          <div className="text-xs text-gray-500">Prayers</div>
          <div className="flex gap-1.5" aria-label={`${me.prayer_count} of ${me.prayer_limit} prayers`}>
            {Array.from({ length: me.prayer_limit }, (_, index) => (
              <span
                key={index}
                className={`h-2.5 w-2.5 rounded-full ${
                  index < me.prayer_count ? 'bg-sky-500' : 'bg-gray-200 dark:bg-gray-700'
                }`}
              />
            ))}
          </div>
        </div>
      </section>

      <section className="rounded-2xl border border-gray-200 bg-white p-5 shadow-xs dark:border-gray-800 dark:bg-gray-900">
        <h2 className="font-display text-base font-semibold text-gray-900 dark:text-white">Today</h2>
        {sessions.length === 0 ? (
          <p className="mt-3 text-sm text-gray-500">Nothing logged today.</p>
        ) : (
          <ul className="mt-3 divide-y divide-gray-100 dark:divide-gray-800">
            {sessions.map((session) => (
              <li key={session.away_id} className="flex items-center justify-between gap-3 py-3">
                <div className="min-w-0">
                  <div className="text-sm font-semibold text-gray-900 dark:text-white">
                    {KIND_LABEL[session.kind]}
                    {session.kind === 'meeting' && session.with_whom ? ` with ${session.with_whom}` : ''}
                  </div>
                  <div className="text-xs text-gray-500">{sessionRange(session, now)}</div>
                </div>
                <Badge tone={TONE[session.kind]}>{session.ended_at ? 'Done' : 'Open'}</Badge>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
