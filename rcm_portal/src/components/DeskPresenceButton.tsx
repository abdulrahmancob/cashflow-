import { useEffect, useState } from 'react'
import { deskIdleState, enableDeskIdle, subscribeDeskIdle } from '../auth/deskIdle'
import { Button } from './ui'

export function DeskPresenceButton() {
  const [desk, setDesk] = useState(deskIdleState)
  const [busy, setBusy] = useState(false)
  const [dismissed, setDismissed] = useState(false)

  useEffect(() => subscribeDeskIdle(setDesk), [])

  if (dismissed) return null
  const needsAllow = desk.supported && desk.permission === 'prompt'
  if (!needsAllow && desk.supported) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-gray-900/40 p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="desk-presence-title"
        className="w-full max-w-md rounded-2xl border border-gray-200 bg-white p-5 shadow-xl dark:border-gray-700 dark:bg-gray-900"
      >
        <h2 id="desk-presence-title" className="font-display text-lg font-semibold text-gray-900 dark:text-white">
          {needsAllow ? 'Stay visible at your desk' : 'Open Chrome or Edge'}
        </h2>
        <p className="mt-2 text-sm text-gray-600 dark:text-gray-300">
          {needsAllow
            ? 'Chrome will ask once whether this site can tell when you are using this computer. The away board uses that to see who is at their desk, including while this tab is in the background. It does not see other sites or what you type.'
            : 'This browser cannot tell when you are at your desk. Open Remitarc in Chrome or Edge and choose Allow. While this tab is in front, you still count as at your desk.'}
        </p>
        <div className="mt-4 flex items-center justify-end gap-2">
          {needsAllow ? (
            <>
              <Button type="button" variant="ghost" disabled={busy} onClick={() => setDismissed(true)}>
                Not now
              </Button>
              <Button
                type="button"
                disabled={busy}
                onClick={() => {
                  setBusy(true)
                  void enableDeskIdle().finally(() => setBusy(false))
                }}
              >
                Allow
              </Button>
            </>
          ) : (
            <Button type="button" onClick={() => setDismissed(true)}>
              Got it
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}
