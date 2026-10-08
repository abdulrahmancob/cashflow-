export type PingState = 'active' | 'idle' | 'locked' | 'unknown'

export type DetectorView = {
  watching: boolean
  userActive: boolean
  screenLocked: boolean
  idleSince: number | null
}

export type PresenceInputs = {
  visible: boolean
  lastInputAt: number
  now: number
  graceMs: number
  detector: DetectorView
}

const DETECTOR_THRESHOLD_MS = 60_000

/**
 * One state per ping, never silence. A hidden tab without the idle detector cannot see
 * the rest of the computer, so after the grace it says unknown instead of guessing idle.
 */
export function pingState(input: PresenceInputs): PingState {
  const { detector, now } = input
  if (detector.watching && detector.screenLocked) return 'locked'
  if (detector.watching && detector.userActive) return 'active'
  if (now - input.lastInputAt <= input.graceMs) return 'active'
  if (detector.watching) {
    const idleFor =
      detector.idleSince == null ? 0 : now - detector.idleSince + DETECTOR_THRESHOLD_MS
    return idleFor >= input.graceMs ? 'idle' : 'active'
  }
  return input.visible ? 'idle' : 'unknown'
}
