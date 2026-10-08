// Sends one word to the Remitarc away board every 30 seconds: active, idle, locked, or paused.
// Nothing about tabs, sites, or typing is read or sent.

const BASE = 'https://remitarc.cobsolution.cloud'
const PING_URL = `${BASE}/api/desk/ping`
const ALARM = 'desk-ping'
const DEFAULT_GRACE_SECONDS = 300
const VERSION = chrome.runtime.getManifest().version

async function readSettings() {
  return chrome.storage.local.get(['token', 'grace', 'paused', 'name'])
}

function setBadge(text, color) {
  chrome.action.setBadgeText({ text })
  if (color) chrome.action.setBadgeBackgroundColor({ color })
}

async function ensureSchedule() {
  const { grace } = await readSettings()
  chrome.idle.setDetectionInterval(grace || DEFAULT_GRACE_SECONDS)
  const existing = await chrome.alarms.get(ALARM)
  if (!existing) chrome.alarms.create(ALARM, { periodInMinutes: 0.5 })
}

async function ping(knownState) {
  const settings = await readSettings()
  if (!settings.token) {
    setBadge('!', '#e11d48')
    return
  }
  const grace = settings.grace || DEFAULT_GRACE_SECONDS
  const state = settings.paused ? 'paused' : knownState || (await chrome.idle.queryState(grace))
  try {
    const res = await fetch(PING_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Device ${settings.token}`,
      },
      body: JSON.stringify({ state, version: VERSION, client_at: new Date().toISOString() }),
    })
    if (res.status === 401) {
      await chrome.storage.local.remove(['token', 'name'])
      await chrome.storage.local.set({ lastError: 'This computer was disconnected. Connect it again from My day.' })
      setBadge('!', '#e11d48')
      return
    }
    if (res.status === 429) return
    if (!res.ok) {
      await chrome.storage.local.set({ lastError: `The portal answered ${res.status}.` })
      return
    }
    const data = await res.json()
    const update = {
      lastSentAt: Date.now(),
      lastState: state,
      lastError: null,
      name: data.display_name || settings.name || '',
    }
    const nextGrace = Number(data.idle_grace_seconds)
    if (Number.isFinite(nextGrace) && nextGrace >= 60 && nextGrace !== settings.grace) {
      update.grace = nextGrace
      chrome.idle.setDetectionInterval(nextGrace)
    }
    await chrome.storage.local.set(update)
    setBadge(settings.paused ? 'II' : '', '#6b7280')
  } catch {
    await chrome.storage.local.set({ lastError: 'No connection. It will retry in 30 seconds.' })
  }
}

chrome.runtime.onInstalled.addListener(() => {
  void ensureSchedule().then(() => ping())
})

chrome.runtime.onStartup.addListener(() => {
  void ensureSchedule().then(() => ping())
})

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) void ping()
})

chrome.idle.onStateChanged.addListener((state) => {
  void ping(state)
})

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  const fromPortal = sender.origin === BASE || (sender.url || '').startsWith(`${BASE}/`)
  if (message?.type === 'pair' && fromPortal && typeof message.token === 'string') {
    void chrome.storage.local
      .set({ token: message.token, paused: false, lastError: null })
      .then(ensureSchedule)
      .then(() => ping())
      .then(() => reply({ ok: true }))
    return true
  }
  if (message?.type === 'status') {
    void readSettings().then((settings) => reply({ paired: !!settings.token, paused: !!settings.paused }))
    return true
  }
  if (message?.type === 'pause' || message?.type === 'resume') {
    void chrome.storage.local
      .set({ paused: message.type === 'pause' })
      .then(() => ping())
      .then(() => reply({ ok: true }))
    return true
  }
  return false
})
