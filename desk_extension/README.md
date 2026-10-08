# Remitarc Desk Tracker

A Chrome and Edge extension (Manifest V3) that tells the away board whether a person is
`active`, `idle`, `locked`, or `paused`. Nothing else is read or sent. See
`rcm_portal/public/desk-tracker-privacy.html`.

## How it works
- `background.js` asks `chrome.idle` for the state every 30 seconds (`chrome.alarms`) and
  whenever it changes, and posts it to `POST /api/desk/ping` with `Authorization: Device <token>`.
- Idle means no input for the server's grace (default 5 minutes, `CASHFLOW_IDLE_GRACE_SECONDS`),
  so a phone call or reading a long EOB does not count as idle.
- Pairing needs no extension ID. `content.js` runs only on the portal. My day asks the server
  for a device token (`POST /api/desk/devices`) and hands it to the extension with `postMessage`.
  The server keeps only a SHA-256 hash of the token. The token works only on `/api/desk/ping`.
- A 401 clears the token and shows a red `!` badge, so a revoked computer has to be connected again.

## Pilot install (Load unpacked)
1. Zip this folder, or copy it to the computer.
2. Open `chrome://extensions` (Edge: `edge://extensions`) and turn on Developer mode.
3. Choose **Load unpacked** and select the `desk_extension` folder.
4. Open Remitarc, go to **My day**, and choose **Connect this computer**.

Chrome shows a developer-mode notice for unpacked extensions. Publishing removes it.

## Publishing
- Chrome Web Store: publish as **Unlisted**. Add 128 px icons and point the privacy field at
  `https://remitarc.cobsolution.cloud/desk-tracker-privacy.html`.
- Edge Add-ons: upload the same zip.
- Put the store links in `DESK_TRACKER_LINKS` in `rcm_portal/src/components/DeskTrackerConnect.tsx`.

## Limits
- Nothing runs when the browser is fully closed. The board then says Offline (no signal).
- It reports the computer, not the person. A personal computer used for other things while
  active still counts as active.
