import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { deskIdleState, enableDeskIdle, subscribeDeskIdle } from '../auth/deskIdle'
import { Button } from './ui'

const DISMISS_KEY = 'rcm-desk-idle-dismissed'

function dismissedToday() {
  try {
    return localStorage.getItem(DISMISS_KEY) === new Date().toDateString()
  } catch {
    return false
  }
}

function rememberDismiss() {
  try {
    localStorage.setItem(DISMISS_KEY, new Date().toDateString())
  } catch {
    /* private window: the card comes back on the next load */
  }
}

/** A corner card, never a blocking dialog. It only asks when Chrome or Edge can still allow it. */
export function DeskPresenceButton() {
  const [desk, setDesk] = useState(deskIdleState)
  const [busy, setBusy] = useState(false)
  const [dismissed, setDismissed] = useState(dismissedToday)

  useEffect(() => subscribeDeskIdle(setDesk), [])

  const needsAllow = desk.supported && desk.permission === 'prompt' && !desk.watching
  const failed = desk.supported && desk.failed
  if (dismissed || (!needsAllow && !failed)) return null

  return (
    <div
      role="status"
      className="fixed bottom-4 right-4 z-40 w-80 rounded-2xl border border-gray-200 bg-white p-4 shadow-xl dark:border-gray-700 dark:bg-gray-900"
    >
      <div className="font-display text-sm font-semibold text-gray-900 dark:text-white">
        {failed ? 'Idle detection did not start' : 'Show the board when you are at your computer'}
      </div>
      <p className="mt-1 text-xs text-gray-600 dark:text-gray-300">
        {failed
          ? 'Reload this page. If it keeps failing, install the desk tracker from My day.'
          : 'Chrome asks once. It only tells the board whether you are active, idle, or locked. It never sees other sites or what you type.'}
      </p>
      <div className="mt-3 flex items-center justify-between gap-2">
        <Link to="/my-day" className="text-xs font-medium text-brand-700 underline dark:text-brand-300">
          Desk tracker
        </Link>
        <div className="flex gap-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={busy}
            onClick={() => {
              rememberDismiss()
              setDismissed(true)
            }}
          >
            Not now
          </Button>
          {needsAllow && (
            <Button
              type="button"
              size="sm"
              disabled={busy}
              onClick={() => {
                setBusy(true)
                void enableDeskIdle().finally(() => setBusy(false))
              }}
            >
              Allow
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}
