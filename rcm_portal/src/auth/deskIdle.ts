const DEVICE_IDLE_MS = 60_000

type IdlePermission = 'unknown' | 'granted' | 'denied' | 'prompt'

type IdleDetectorInstance = EventTarget & {
  userState: 'active' | 'idle' | null
  screenState: 'locked' | 'unlocked' | null
  start: (options?: { threshold?: number; signal?: AbortSignal }) => Promise<void>
}

type IdleDetectorCtor = {
  new (): IdleDetectorInstance
  requestPermission: () => Promise<PermissionState>
}

export type DeskIdleState = {
  supported: boolean
  permission: IdlePermission
  watching: boolean
  userActive: boolean
}

const listeners = new Set<(state: DeskIdleState) => void>()

let state: DeskIdleState = {
  supported: typeof window !== 'undefined' && idleDetectorCtor() !== null,
  permission: 'unknown',
  watching: false,
  userActive: false,
}

let starting: Promise<void> | null = null

function idleDetectorCtor(): IdleDetectorCtor | null {
  const ctor = (window as Window & { IdleDetector?: IdleDetectorCtor }).IdleDetector
  return ctor ?? null
}

function publish(patch: Partial<DeskIdleState>) {
  state = { ...state, ...patch }
  for (const listener of listeners) listener(state)
}

export function deskIdleState() {
  return state
}

export function deviceKeepsPresence() {
  return state.watching && state.userActive
}

export function deviceReportsIdle() {
  return state.watching && !state.userActive
}

export function subscribeDeskIdle(listener: (next: DeskIdleState) => void) {
  listeners.add(listener)
  listener(state)
  return () => {
    listeners.delete(listener)
  }
}

function applyDetector(detector: IdleDetectorInstance) {
  const userActive = detector.userState === 'active' && detector.screenState !== 'locked'
  publish({ watching: true, permission: 'granted', userActive })
}

async function startDetector(Ctor: IdleDetectorCtor) {
  if (state.watching) return
  if (starting) return starting
  starting = (async () => {
    const detector = new Ctor()
    detector.addEventListener('change', () => applyDetector(detector))
    await detector.start({ threshold: DEVICE_IDLE_MS })
    applyDetector(detector)
  })().finally(() => {
    starting = null
  })
  return starting
}

export async function primeDeskIdle() {
  const Ctor = idleDetectorCtor()
  if (!Ctor) {
    publish({ supported: false })
    return
  }
  publish({ supported: true })
  try {
    const status = await navigator.permissions.query({
      name: 'idle-detection' as PermissionName,
    })
    if (status.state === 'granted') {
      try {
        await startDetector(Ctor)
      } catch {
        publish({ permission: 'granted', watching: false, userActive: false })
      }
      return
    }
    publish({ permission: status.state === 'denied' ? 'denied' : 'prompt' })
  } catch {
    publish({ permission: 'prompt' })
  }
}

export async function enableDeskIdle() {
  const Ctor = idleDetectorCtor()
  if (!Ctor) {
    publish({ supported: false })
    return
  }
  const permission = await Ctor.requestPermission()
  if (permission !== 'granted') {
    publish({ permission: 'denied', watching: false, userActive: false })
    return
  }
  try {
    await startDetector(Ctor)
  } catch {
    publish({ permission: 'granted', watching: false, userActive: false })
  }
}
