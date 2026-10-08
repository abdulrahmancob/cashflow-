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
  failed: boolean
  userActive: boolean
  screenLocked: boolean
  idleSince: number | null
}

const listeners = new Set<(state: DeskIdleState) => void>()

let state: DeskIdleState = {
  supported: typeof window !== 'undefined' && idleDetectorCtor() !== null,
  permission: 'unknown',
  watching: false,
  failed: false,
  userActive: false,
  screenLocked: false,
  idleSince: null,
}

let starting: Promise<void> | null = null
let abort: AbortController | null = null
let permissionStatus: PermissionStatus | null = null

function idleDetectorCtor(): IdleDetectorCtor | null {
  if (typeof window === 'undefined') return null
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

export type DeskPermissionReport =
  | 'watching'
  | 'prompt'
  | 'denied'
  | 'unsupported'
  | 'granted_not_watching'
  | 'error'

/** What the board should show. Unknown means not checked yet, so nothing is reported. */
export function deskPermissionReport(): DeskPermissionReport | undefined {
  if (!state.supported) return 'unsupported'
  if (state.watching) return 'watching'
  if (state.failed) return 'error'
  if (state.permission === 'granted') return 'granted_not_watching'
  if (state.permission === 'denied') return 'denied'
  if (state.permission === 'prompt') return 'prompt'
  return undefined
}

export function subscribeDeskIdle(listener: (next: DeskIdleState) => void) {
  listeners.add(listener)
  listener(state)
  return () => {
    listeners.delete(listener)
  }
}

function applyDetector(detector: IdleDetectorInstance) {
  const userActive = detector.userState === 'active'
  const screenLocked = detector.screenState === 'locked'
  publish({
    watching: true,
    failed: false,
    permission: 'granted',
    userActive,
    screenLocked,
    idleSince: userActive ? null : (state.idleSince ?? Date.now()),
  })
}

async function startDetector(Ctor: IdleDetectorCtor) {
  if (state.watching) return
  if (starting) return starting
  starting = (async () => {
    const controller = new AbortController()
    const detector = new Ctor()
    detector.addEventListener('change', () => applyDetector(detector))
    await detector.start({ threshold: DEVICE_IDLE_MS, signal: controller.signal })
    abort = controller
    applyDetector(detector)
  })()
    .catch(() => {
      publish({ watching: false, failed: true, userActive: false, screenLocked: false })
    })
    .finally(() => {
      starting = null
    })
  return starting
}

export function stopDeskIdle() {
  abort?.abort()
  abort = null
  publish({ watching: false, userActive: false, screenLocked: false, idleSince: null })
}

async function readPermission(Ctor: IdleDetectorCtor) {
  const status = permissionStatus
  if (!status) return
  if (status.state === 'granted') {
    publish({ permission: 'granted' })
    await startDetector(Ctor)
    return
  }
  if (state.watching) stopDeskIdle()
  publish({ permission: status.state === 'denied' ? 'denied' : 'prompt', failed: false })
}

/** Check permission, start watching when granted, and follow later changes to it. */
export async function primeDeskIdle() {
  const Ctor = idleDetectorCtor()
  if (!Ctor) {
    publish({ supported: false })
    return
  }
  publish({ supported: true })
  try {
    if (!permissionStatus) {
      permissionStatus = await navigator.permissions.query({
        name: 'idle-detection' as PermissionName,
      })
      permissionStatus.addEventListener('change', () => void readPermission(Ctor))
    }
    await readPermission(Ctor)
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
  let permission: PermissionState
  try {
    permission = await Ctor.requestPermission()
  } catch {
    publish({ failed: true })
    return
  }
  if (permission !== 'granted') {
    publish({ permission: 'denied', watching: false, userActive: false })
    return
  }
  publish({ permission: 'granted' })
  await startDetector(Ctor)
}
