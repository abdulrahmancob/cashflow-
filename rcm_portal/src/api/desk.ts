import { api } from './client'

export type DeskDevice = {
  device_id: string
  label: string | null
  user_agent: string | null
  created_at: string
  last_seen_at: string | null
}

export function pairDeskDevice(label: string) {
  return api<{ device_id: string; token: string }>('/api/desk/devices', {
    method: 'POST',
    body: JSON.stringify({ label }),
  })
}

export function fetchDeskDevices() {
  return api<{ devices: DeskDevice[] }>('/api/desk/devices')
}

export function unpairDeskDevice(deviceId: string) {
  return api<{ ok: boolean }>(`/api/desk/devices/${encodeURIComponent(deviceId)}`, {
    method: 'DELETE',
  })
}
