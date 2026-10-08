import { LIVE_LABEL, trackerNote, type LiveStatus, type MyPresence } from '../api/away'
import { Badge } from './ui'

const TONE: Record<LiveStatus, 'green' | 'amber' | 'blue' | 'gray'> = {
  working: 'green',
  idle: 'amber',
  locked: 'amber',
  unverified: 'blue',
  paused: 'gray',
  signed_out: 'gray',
  offline: 'gray',
}

function hint(presence: MyPresence) {
  const minutes = Math.round(presence.idle_grace_seconds / 60)
  switch (presence.status) {
    case 'working':
      return 'You count as working.'
    case 'idle':
      return `No keyboard or mouse for ${minutes} minutes. Use the computer and it switches back.`
    case 'locked':
      return 'Your screen is locked.'
    case 'unverified':
      return 'This tab is in the background and the browser cannot see your other apps. Install the desk tracker below so work in WebPT, Excel, or calls counts.'
    case 'paused':
      return 'You paused the desk tracker. Resume it from the extension icon.'
    default:
      return 'Waiting for the first signal from this computer.'
  }
}

/** Shows each person exactly what the away board shows for them, and which tracker feeds it. */
export function BoardPresenceCard({
  presence,
  now,
  children,
}: {
  presence: MyPresence | undefined
  now: number
  children?: React.ReactNode
}) {
  if (!presence) return null
  const tracker = trackerNote(presence.tracker, now)
  return (
    <section className="rounded-2xl border border-gray-200 bg-white p-5 shadow-xs dark:border-gray-800 dark:bg-gray-900">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-wider text-gray-500">
            The away board sees you as
          </div>
          <div className="mt-2">
            <Badge tone={TONE[presence.status]}>{LIVE_LABEL[presence.status]}</Badge>
          </div>
        </div>
        <div
          className={`text-xs font-medium ${
            tracker.ok ? 'text-emerald-700 dark:text-emerald-300' : 'text-rose-600 dark:text-rose-300'
          }`}
        >
          {tracker.text}
        </div>
      </div>
      <p className="mt-3 text-sm text-gray-600 dark:text-gray-300">{hint(presence)}</p>
      {children}
    </section>
  )
}
