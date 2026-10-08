import { useEffect, useRef } from 'react'
import { useLocation } from 'react-router-dom'
import {
  deskIdleState,
  deskPermissionReport,
  primeDeskIdle,
  stopDeskIdle,
  subscribeDeskIdle,
} from './deskIdle'
import { pingState } from './presenceState'

const HEARTBEAT_URL = '/api/analytics/heartbeat'
const CHANNEL = 'rcm-presence'
const LEADER_LOCK = 'rcm-presence-leader'
const PING_MS = 30_000
const SHARE_MS = 10_000
const INPUT_SHARE_MS = 5_000
const PEER_FRESH_MS = 25_000
const DEFAULT_GRACE_MS = 5 * 60_000
const SIGNED_OUT_PREFIX = '⚠ Signed out · '

type Peer = {
  tabId: string
  visible: boolean
  lastInputAt: number
  page: string
  at: number
}

type Message =
  | ({ type: 'tab'; wake?: boolean } & Peer)
  | { type: 'closing'; tabId: string }

let graceMs = DEFAULT_GRACE_MS
let closeCurrentTab: (() => void) | null = null

function newTabId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

function markSignedOut() {
  if (!document.title.startsWith(SIGNED_OUT_PREFIX)) {
    document.title = `${SIGNED_OUT_PREFIX}${document.title}`
  }
  window.dispatchEvent(new Event('rcm-signed-out'))
  if ('Notification' in window && Notification.permission === 'granted') {
    try {
      new Notification('Remitarc: you are signed out', {
        body: 'Sign in again so the away board sees you as working.',
        tag: 'rcm-signed-out',
      })
    } catch {
      /* some browsers only allow notifications from a service worker */
    }
  }
}

function clearSignedOut() {
  if (document.title.startsWith(SIGNED_OUT_PREFIX)) {
    document.title = document.title.slice(SIGNED_OUT_PREFIX.length)
  }
}

async function send(body: Record<string, unknown>) {
  try {
    const res = await fetch(HEARTBEAT_URL, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      keepalive: true,
    })
    if (res.status === 401) {
      markSignedOut()
      return
    }
    if (!res.ok) return
    clearSignedOut()
    const data = (await res.json().catch(() => null)) as { idle_grace_seconds?: number } | null
    const grace = Number(data?.idle_grace_seconds)
    if (Number.isFinite(grace) && grace >= 60) graceMs = grace * 1000
  } catch {
    /* offline: the next tick or the online event sends again */
  }
}

/** Tell the server this tab is gone before the session cookie is cleared. */
export function announceTabClosed() {
  closeCurrentTab?.()
}

/**
 * One tab per browser holds a Web Lock and pings for all of them. The others share their
 * visibility and last input over a BroadcastChannel. Every ping carries a state, so a
 * hidden or quiet tab is never mistaken for a closed one.
 */
export function useActivityHeartbeat() {
  const location = useLocation()
  const pathRef = useRef(location.pathname)
  pathRef.current = location.pathname

  useEffect(() => {
    const tabId = newTabId()
    const peers = new Map<string, Peer>()
    const channel = 'BroadcastChannel' in window ? new BroadcastChannel(CHANNEL) : null
    const locks = (navigator as Navigator & { locks?: LockManager }).locks
    let leader = !locks
    let releaseLeader: (() => void) | null = null
    let lastInputAt = Date.now()
    let lastShared = 0
    let stopped = false
    let closedSent = false

    const visible = () => document.visibilityState === 'visible'
    const self = (): Peer => ({
      tabId,
      visible: visible(),
      lastInputAt,
      page: pathRef.current,
      at: Date.now(),
    })

    const share = (wake = false) => {
      lastShared = Date.now()
      channel?.postMessage({ type: 'tab', wake, ...self() } satisfies Message)
    }

    const combined = () => {
      const now = Date.now()
      let anyVisible = visible()
      let input = lastInputAt
      let page: string | null = anyVisible ? pathRef.current : null
      for (const peer of [...peers.values()]) {
        if (now - peer.at > PEER_FRESH_MS) {
          peers.delete(peer.tabId)
          continue
        }
        if (peer.lastInputAt > input) input = peer.lastInputAt
        if (peer.visible) {
          anyVisible = true
          if (!page) page = peer.page
        }
      }
      return { anyVisible, input, page }
    }

    const ping = () => {
      if (stopped || !leader) return
      const now = Date.now()
      const view = combined()
      const state = pingState({
        visible: view.anyVisible,
        lastInputAt: view.input,
        now,
        graceMs,
        detector: deskIdleState(),
      })
      void send({
        state,
        visible: view.anyVisible,
        page_path: view.page ?? undefined,
        tab_id: tabId,
        client_at: new Date(now).toISOString(),
        desk_permission: deskPermissionReport(),
      })
    }

    const wake = () => {
      if (leader) ping()
      else share(true)
    }

    const onInput = () => {
      const now = Date.now()
      const wasQuiet = now - lastInputAt > graceMs
      lastInputAt = now
      if (wasQuiet) wake()
      else if (now - lastShared > INPUT_SHARE_MS) share()
    }

    const sendClosed = () => {
      if (closedSent || !leader) return
      const othersOpen = [...peers.values()].some((peer) => Date.now() - peer.at <= PEER_FRESH_MS)
      if (othersOpen) return
      closedSent = true
      const body = new Blob(
        [JSON.stringify({ closed: true, tab_id: tabId, desk_permission: deskPermissionReport() })],
        { type: 'application/json' },
      )
      if (!navigator.sendBeacon(HEARTBEAT_URL, body)) void send({ closed: true, tab_id: tabId })
    }
    closeCurrentTab = sendClosed

    if (channel) {
      channel.onmessage = (event: MessageEvent<Message>) => {
        const message = event.data
        if (!message || message.tabId === tabId) return
        if (message.type === 'closing') {
          peers.delete(message.tabId)
          return
        }
        const { type: _type, wake: wakeUp, ...peer } = message
        void _type
        peers.set(peer.tabId, peer)
        if (wakeUp && leader) ping()
      }
    }

    if (locks) {
      locks
        .request(
          LEADER_LOCK,
          () =>
            new Promise<void>((resolve) => {
              if (stopped) {
                resolve()
                return
              }
              releaseLeader = resolve
              leader = true
              ping()
            }),
        )
        .catch(() => {
          leader = true
        })
    }

    const inputOptions: AddEventListenerOptions = { passive: true, capture: true }
    const inputEvents = ['pointerdown', 'keydown', 'wheel', 'mousemove', 'touchstart'] as const
    for (const name of inputEvents) window.addEventListener(name, onInput, inputOptions)
    document.addEventListener('scroll', onInput, inputOptions)

    const onVisibility = () => {
      share(visible())
      if (leader) ping()
    }
    const onHide = () => {
      channel?.postMessage({ type: 'closing', tabId } satisfies Message)
      sendClosed()
    }
    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('online', wake)
    window.addEventListener('focus', wake)
    document.addEventListener('resume', wake)
    window.addEventListener('pagehide', onHide)

    let lastDesk = deskIdleState()
    const unsubscribeDesk = subscribeDeskIdle((next) => {
      const changed =
        next.watching !== lastDesk.watching ||
        next.userActive !== lastDesk.userActive ||
        next.screenLocked !== lastDesk.screenLocked
      lastDesk = next
      if (changed && leader) ping()
    })

    void primeDeskIdle().finally(() => {
      if (!stopped) ping()
    })
    share()
    const pingTimer = window.setInterval(ping, PING_MS)
    const shareTimer = window.setInterval(() => share(), SHARE_MS)

    return () => {
      sendClosed()
      stopped = true
      closeCurrentTab = null
      window.clearInterval(pingTimer)
      window.clearInterval(shareTimer)
      for (const name of inputEvents) window.removeEventListener(name, onInput, inputOptions)
      document.removeEventListener('scroll', onInput, inputOptions)
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('online', wake)
      window.removeEventListener('focus', wake)
      document.removeEventListener('resume', wake)
      window.removeEventListener('pagehide', onHide)
      unsubscribeDesk()
      stopDeskIdle()
      channel?.postMessage({ type: 'closing', tabId } satisfies Message)
      channel?.close()
      releaseLeader?.()
    }
  }, [])
}
