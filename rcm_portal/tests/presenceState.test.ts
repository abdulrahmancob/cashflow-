// Run with: node --test tests/presenceState.test.ts
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { pingState, type DetectorView } from '../src/auth/presenceState.ts'

const NOW = 1_000_000_000
const GRACE = 5 * 60_000
const OFF: DetectorView = { watching: false, userActive: false, screenLocked: false, idleSince: null }

function state(overrides: Partial<Parameters<typeof pingState>[0]>) {
  return pingState({ visible: true, lastInputAt: NOW, now: NOW, graceMs: GRACE, detector: OFF, ...overrides })
}

test('recent portal input is active, visible or not', () => {
  assert.equal(state({ lastInputAt: NOW - 60_000 }), 'active')
  assert.equal(state({ visible: false, lastInputAt: NOW - 60_000 }), 'active')
})

test('without the detector a quiet visible tab is idle and a hidden one is unknown', () => {
  assert.equal(state({ lastInputAt: NOW - GRACE - 1 }), 'idle')
  assert.equal(state({ visible: false, lastInputAt: NOW - GRACE - 1 }), 'unknown')
})

test('the detector sees work in other apps and locks', () => {
  const working: DetectorView = { watching: true, userActive: true, screenLocked: false, idleSince: null }
  assert.equal(state({ visible: false, lastInputAt: 0, detector: working }), 'active')
  const locked: DetectorView = { ...working, userActive: false, screenLocked: true, idleSince: NOW }
  assert.equal(state({ detector: locked }), 'locked')
})

test('detector idle only counts after the grace, so a phone call is not idle', () => {
  const quiet = (since: number): DetectorView => ({
    watching: true,
    userActive: false,
    screenLocked: false,
    idleSince: since,
  })
  assert.equal(state({ lastInputAt: 0, detector: quiet(NOW - 2 * 60_000) }), 'active')
  assert.equal(state({ lastInputAt: 0, detector: quiet(NOW - 4 * 60_000) }), 'idle')
})

test('portal input beats a stale detector idle', () => {
  const quiet: DetectorView = { watching: true, userActive: false, screenLocked: false, idleSince: NOW - GRACE * 2 }
  assert.equal(state({ lastInputAt: NOW - 10_000, detector: quiet }), 'active')
})
