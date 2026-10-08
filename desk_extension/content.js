// Runs only on the Remitarc portal. It lets My day find the extension and hand it a device token.

const ORIGIN = window.location.origin
const VERSION = chrome.runtime.getManifest().version

function announce() {
  chrome.runtime.sendMessage({ type: 'status' }, (status) => {
    window.postMessage(
      {
        source: 'rcm-desk-ext',
        type: 'hello',
        version: VERSION,
        paired: !!status?.paired,
        paused: !!status?.paused,
      },
      ORIGIN,
    )
  })
}

window.addEventListener('message', (event) => {
  if (event.source !== window || event.origin !== ORIGIN) return
  const data = event.data
  if (!data || data.target !== 'rcm-desk-ext') return
  if (data.type === 'hello') {
    announce()
    return
  }
  if (data.type === 'pair' && typeof data.token === 'string' && data.token.length >= 20) {
    chrome.runtime.sendMessage({ type: 'pair', token: data.token }, (reply) => {
      window.postMessage({ source: 'rcm-desk-ext', type: 'paired', ok: !!reply?.ok }, ORIGIN)
    })
  }
})

announce()
