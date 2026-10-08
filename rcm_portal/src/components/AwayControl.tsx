import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { Coffee, Users, X } from 'lucide-react'
import {
  endAway,
  fetchAwayMe,
  formatClock,
  liveWarning,
  sessionElapsed,
  startAway,
  type AwayKind,
  type AwayMe,
  type AwayWarning,
} from '../api/away'
import { ApiError } from '../api/client'
import { Button, Field, Input } from './ui'

const POLL_MS = 15_000

const KIND_LABEL: Record<AwayKind, string> = {
  break: 'Break',
  prayer: 'Prayer',
  meeting: 'Meeting',
}

type AwayContextValue = {
  me: AwayMe | null
  now: number
  error: string | null
  busy: boolean
  warning: AwayWarning
  run: (action: () => Promise<AwayMe>) => Promise<boolean>
  setError: (message: string | null) => void
}

const AwayContext = createContext<AwayContextValue | null>(null)

function bannerCopy(kind: AwayKind, level: Exclude<AwayWarning, 'none'>) {
  if (kind === 'break' && level === 'over') {
    return 'Your 45-minute break is used up. Please get back to your desk.'
  }
  if (kind === 'break') return 'Less than 5 minutes of break left today.'
  if (kind === 'prayer' && level === 'over') {
    return 'This prayer break is past its time. Please get back to your desk.'
  }
  if (kind === 'prayer') return 'Prayer break is almost over.'
  if (level === 'over') return 'This meeting ran past the time you entered.'
  return 'This meeting is almost over.'
}

export function useAway() {
  const value = useContext(AwayContext)
  if (!value) throw new Error('Away controls must sit inside AwayProvider')
  return value
}

export function AwayProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<AwayMe | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const row = await fetchAwayMe()
        if (!cancelled) setMe(row)
      } catch {
        /* keep the last snapshot if the refresh fails */
      }
    }
    void load()
    const poll = window.setInterval(() => void load(), POLL_MS)
    const tick = window.setInterval(() => setNow(Date.now()), 1000)
    return () => {
      cancelled = true
      window.clearInterval(poll)
      window.clearInterval(tick)
    }
  }, [])

  async function run(action: () => Promise<AwayMe>) {
    setBusy(true)
    setError(null)
    try {
      setMe(await action())
      return true
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not update your status')
      return false
    } finally {
      setBusy(false)
    }
  }

  const warning: AwayWarning = me ? liveWarning(me, now) : 'none'
  return (
    <AwayContext.Provider value={{ me, now, error, busy, warning, run, setError }}>
      {children}
    </AwayContext.Provider>
  )
}

export function AwayBanner() {
  const { me, warning } = useAway()
  if (!me?.open || warning === 'none') return null
  const over = warning === 'over'
  return (
    <div
      role="status"
      className={
        over
          ? 'animate-pulse bg-rose-600 px-4 py-2.5 text-center text-sm font-semibold text-white'
          : 'border-b border-amber-200 bg-amber-100 px-4 py-2.5 text-center text-sm font-semibold text-amber-950'
      }
    >
      {bannerCopy(me.open.kind, warning)}
    </div>
  )
}

export function AwayControl() {
  const { me, now, error, busy, warning, run, setError } = useAway()
  const [open, setOpen] = useState(false)
  const [meetingMinutes, setMeetingMinutes] = useState('30')
  const [withWhom, setWithWhom] = useState('')
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    function onPointer(event: MouseEvent) {
      if (!panelRef.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onPointer)
    return () => document.removeEventListener('mousedown', onPointer)
  }, [open])

  if (!me) return null

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
    if (ok) {
      setOpen(false)
      setWithWhom('')
    }
  }

  if (me.open) {
    const elapsed = sessionElapsed(me.open, now)
    const over = warning === 'over'
    return (
      <div
        className={`flex items-center gap-2 rounded-full border px-2 py-1 ${
          over
            ? 'border-rose-300 bg-rose-50 text-rose-800 dark:border-rose-800 dark:bg-rose-950/50 dark:text-rose-100'
            : 'border-gray-200 bg-gray-50 text-gray-800 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100'
        }`}
      >
        <span className="px-1 text-xs font-semibold uppercase tracking-wide">
          {KIND_LABEL[me.open.kind]}
        </span>
        <span className="font-display text-sm font-semibold tabular-nums">{formatClock(elapsed)}</span>
        <Button
          type="button"
          size="sm"
          variant={over ? 'danger' : 'secondary'}
          disabled={busy}
          onClick={() => void run(() => endAway())}
        >
          I'm back
        </Button>
      </div>
    )
  }

  const breakLeft = `${Math.ceil(me.break_remaining_seconds / 60)} min`
  const prayersLeft = Math.max(0, me.prayer_limit - me.prayer_count)

  return (
    <div className="relative" ref={panelRef}>
      <Button type="button" size="sm" variant="secondary" onClick={() => setOpen((v) => !v)}>
        <Coffee className="h-4 w-4" />
        Step away
      </Button>
      {open && (
        <div className="absolute right-0 z-30 mt-2 max-h-[min(32rem,calc(100dvh-5rem))] w-[22rem] overflow-y-auto rounded-xl border border-gray-200 bg-white p-3 shadow-xl dark:border-gray-700 dark:bg-gray-900">
          <div className="mb-2 flex items-center justify-between">
            <div className="text-sm font-semibold text-gray-900 dark:text-white">Step away</div>
            <button
              type="button"
              className="rounded-md p-1 text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800"
              onClick={() => setOpen(false)}
              aria-label="Close"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
          {error && <p className="mb-2 text-sm text-rose-600">{error}</p>}
          <div className="space-y-2">
            <section className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
              <div className="text-sm font-semibold text-gray-900 dark:text-white">Break</div>
              <p className="mt-0.5 text-xs text-gray-500">
                {breakLeft} left of 45 today. Split it however you need.
              </p>
              <Button
                type="button"
                size="sm"
                className="mt-2"
                disabled={busy}
                onClick={() => void begin('break')}
              >
                Start break
              </Button>
            </section>
            <section className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
              <div className="text-sm font-semibold text-gray-900 dark:text-white">Prayer</div>
              <p className="mt-0.5 text-xs text-gray-500">
                {me.prayer_count} of {me.prayer_limit} today. Each one is 10 minutes.
              </p>
              <Button
                type="button"
                size="sm"
                className="mt-2"
                disabled={busy || prayersLeft === 0}
                onClick={() => void begin('prayer')}
              >
                {prayersLeft === 0 ? 'No prayers left' : 'Start prayer'}
              </Button>
            </section>
            <section className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
              <div className="flex items-center gap-1.5 text-sm font-semibold text-gray-900 dark:text-white">
                <Users className="h-3.5 w-3.5" />
                Meeting
              </div>
              <div className="mt-2 grid grid-cols-2 gap-2">
                <Field label="Minutes">
                  <Input
                    type="number"
                    min={1}
                    value={meetingMinutes}
                    onChange={(e) => setMeetingMinutes(e.target.value)}
                  />
                </Field>
                <Field label="With">
                  <Input
                    value={withWhom}
                    placeholder="Name"
                    onChange={(e) => setWithWhom(e.target.value)}
                  />
                </Field>
              </div>
              <Button
                type="button"
                size="sm"
                className="mt-2"
                disabled={busy}
                onClick={() => void begin('meeting')}
              >
                Start meeting
              </Button>
            </section>
          </div>
        </div>
      )}
    </div>
  )
}
