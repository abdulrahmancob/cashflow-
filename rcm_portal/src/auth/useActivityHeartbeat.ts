import { useEffect, useRef } from 'react'
import { useLocation } from 'react-router-dom'
import { api } from '../api/client'
import { isAwayIdle } from './awayIdle'
import { deskPermissionReport, deviceKeepsPresence, deviceReportsIdle, primeDeskIdle } from './deskIdle'

const PING_MS = 30_000
const IDLE_MS = 5 * 60_000

export function useActivityHeartbeat() {
  const location = useLocation()
  const lastInput = useRef(Date.now())
  const pathRef = useRef(location.pathname)
  pathRef.current = location.pathname

  useEffect(() => {
    const mark = () => {
      lastInput.current = Date.now()
    }
    const opts: AddEventListenerOptions = { passive: true }
    window.addEventListener('pointerdown', mark, opts)
    window.addEventListener('keydown', mark, opts)
    window.addEventListener('scroll', mark, opts)
    window.addEventListener('mousemove', mark, opts)
    return () => {
      window.removeEventListener('pointerdown', mark)
      window.removeEventListener('keydown', mark)
      window.removeEventListener('scroll', mark)
      window.removeEventListener('mousemove', mark)
    }
  }, [])

  useEffect(() => {
    void primeDeskIdle()
    const ping = () => {
      const away = isAwayIdle()
      const hidden = document.visibilityState !== 'visible'
      if (hidden) {
        if (away || !deviceKeepsPresence()) return
        void api('/api/analytics/heartbeat', {
          method: 'POST',
          body: JSON.stringify({ idle: false, presence: true, desk_permission: deskPermissionReport() }),
        }).catch(() => undefined)
        return
      }
      if (!away && (deviceReportsIdle() || Date.now() - lastInput.current > IDLE_MS)) return
      void api('/api/analytics/heartbeat', {
        method: 'POST',
        body: JSON.stringify({
          page_path: pathRef.current,
          idle: away,
          desk_permission: deskPermissionReport(),
        }),
      }).catch(() => undefined)
    }
    ping()
    const t = window.setInterval(ping, PING_MS)
    const onVis = () => {
      ping()
    }
    document.addEventListener('visibilitychange', onVis)
    const onLeave = (event: BeforeUnloadEvent) => {
      const body = new Blob(
        [JSON.stringify({ closed: true, desk_permission: deskPermissionReport() })],
        { type: 'application/json' },
      )
      navigator.sendBeacon('/api/analytics/heartbeat', body)
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', onLeave)
    return () => {
      window.clearInterval(t)
      document.removeEventListener('visibilitychange', onVis)
      window.removeEventListener('beforeunload', onLeave)
    }
  }, [])
}
