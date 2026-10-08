const statusEl = document.getElementById('status')
const detailEl = document.getElementById('detail')
const toggle = document.getElementById('toggle')

function ago(ms) {
  const seconds = Math.max(0, Math.round((Date.now() - ms) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  return `${Math.round(seconds / 60)} min ago`
}

async function render() {
  const s = await chrome.storage.local.get(['token', 'name', 'paused', 'lastSentAt', 'lastState', 'lastError'])
  if (!s.token) {
    statusEl.className = 'bad'
    statusEl.textContent = 'Not connected'
    detailEl.textContent = s.lastError || 'Open My day in Remitarc and choose Connect this computer.'
    toggle.hidden = true
    return
  }
  statusEl.className = s.paused ? 'bad' : 'ok'
  statusEl.textContent = s.paused
    ? `Paused for ${s.name || 'you'}`
    : `Connected as ${s.name || 'you'}`
  const sent = s.lastSentAt ? `Last sent ${ago(s.lastSentAt)}: ${s.lastState}.` : 'Nothing sent yet.'
  detailEl.textContent = s.lastError ? `${sent} ${s.lastError}` : sent
  toggle.hidden = false
  toggle.textContent = s.paused ? 'Resume tracking' : 'Pause tracking'
  toggle.onclick = () => {
    chrome.runtime.sendMessage({ type: s.paused ? 'resume' : 'pause' }, () => void render())
  }
}

void render()
