let away = false

export function setAwayIdle(next: boolean) {
  away = next
}

export function isAwayIdle() {
  return away
}
