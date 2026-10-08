import { useEffect, useState } from 'react'
import { pairDeskDevice } from '../api/desk'
import { ApiError } from '../api/client'
import { Button } from './ui'

/** Store links, once the extension is published. Empty means pilot install only. */
const DESK_TRACKER_LINKS: { label: string; href: string }[] = []

const PRIVACY_URL = '/desk-tracker-privacy.html'
const DETECT_MS = 1500

type Detected = { installed: boolean; paired: boolean; paused: boolean; version: string | null }

type ExtensionMessage = {
  source?: string
  type?: string
  version?: string
  paired?: boolean
  paused?: boolean
  ok?: boolean
}

function deviceLabel() {
  const agent = navigator.userAgent
  const browser = agent.includes('Edg/') ? 'Edge' : agent.includes('Chrome/') ? 'Chrome' : 'Browser'
  const system = agent.includes('Windows') ? 'Windows' : agent.includes('Mac OS') ? 'Mac' : 'Computer'
  return `${browser} on ${system}`
}

/** Finds the desk tracker extension on this computer and connects it to the signed-in person. */
export function DeskTrackerConnect() {
  const [detected, setDetected] = useState<Detected | null>(null)
  const [consent, setConsent] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  useEffect(() => {
    let settled = false
    const onMessage = (event: MessageEvent<ExtensionMessage>) => {
      if (event.source !== window || event.origin !== window.location.origin) return
      const data = event.data
      if (data?.source !== 'rcm-desk-ext') return
      if (data.type === 'hello') {
        settled = true
        setDetected({
          installed: true,
          paired: !!data.paired,
          paused: !!data.paused,
          version: data.version ?? null,
        })
      }
      if (data.type === 'paired') {
        setBusy(false)
        if (data.ok) {
          setDetected((prev) => (prev ? { ...prev, paired: true, paused: false } : prev))
          setMessage('This computer is connected. The board now sees work in your other apps.')
        } else {
          setMessage('The extension did not accept the connection. Reload this page and try again.')
        }
      }
    }
    window.addEventListener('message', onMessage)
    window.postMessage({ target: 'rcm-desk-ext', type: 'hello' }, window.location.origin)
    const timer = window.setTimeout(() => {
      if (!settled) setDetected({ installed: false, paired: false, paused: false, version: null })
    }, DETECT_MS)
    return () => {
      window.removeEventListener('message', onMessage)
      window.clearTimeout(timer)
    }
  }, [])

  async function connect() {
    setBusy(true)
    setMessage(null)
    try {
      const { token } = await pairDeskDevice(deviceLabel())
      window.postMessage({ target: 'rcm-desk-ext', type: 'pair', token }, window.location.origin)
    } catch (e) {
      setBusy(false)
      setMessage(e instanceof ApiError ? e.message : 'Could not connect this computer.')
    }
  }

  if (!detected) return null

  return (
    <div className="mt-4 rounded-xl border border-gray-200 p-4 dark:border-gray-800">
      <div className="text-sm font-semibold text-gray-900 dark:text-white">Desk tracker</div>
      {!detected.installed && (
        <div className="mt-1 space-y-2 text-sm text-gray-600 dark:text-gray-300">
          <p>
            Install the Remitarc Desk Tracker in Chrome or Edge so calls and work in WebPT or Excel
            count, even with this tab closed.
          </p>
          {DESK_TRACKER_LINKS.length > 0 ? (
            <div className="flex flex-wrap gap-3">
              {DESK_TRACKER_LINKS.map((link) => (
                <a
                  key={link.href}
                  href={link.href}
                  target="_blank"
                  rel="noreferrer"
                  className="font-medium text-brand-700 underline dark:text-brand-300"
                >
                  {link.label}
                </a>
              ))}
            </div>
          ) : (
            <p className="text-xs text-gray-500">Ask your lead for the install file.</p>
          )}
        </div>
      )}
      {detected.installed && detected.paired && (
        <p className="mt-1 text-sm text-emerald-700 dark:text-emerald-300">
          {detected.paused
            ? 'Connected, but paused. Resume it from the extension icon.'
            : 'Connected on this computer.'}
        </p>
      )}
      {detected.installed && !detected.paired && (
        <div className="mt-2 space-y-2">
          <label className="flex items-start gap-2 text-sm text-gray-700 dark:text-gray-200">
            <input
              type="checkbox"
              className="mt-1"
              checked={consent}
              onChange={(event) => setConsent(event.target.checked)}
            />
            <span>
              I agree that this computer sends only active, idle, locked, or paused to the away
              board.{' '}
              <a href={PRIVACY_URL} target="_blank" rel="noreferrer" className="underline">
                What it sends
              </a>
            </span>
          </label>
          <Button type="button" size="sm" disabled={!consent || busy} onClick={() => void connect()}>
            {busy ? 'Connecting…' : 'Connect this computer'}
          </Button>
        </div>
      )}
      {message && <p className="mt-2 text-sm text-gray-600 dark:text-gray-300">{message}</p>}
    </div>
  )
}
