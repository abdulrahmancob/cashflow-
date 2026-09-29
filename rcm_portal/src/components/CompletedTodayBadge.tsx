import { useEffect, useState } from 'react'
import { api } from '../api/client'

export const TODAY_REFRESH = 'work-today-refresh'

type Area = 'eligibility' | 'collection' | 'submission'

export function CompletedTodayBadge({
  area,
  compact = false,
}: {
  area: Area
  compact?: boolean
}) {
  const [n, setN] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const row = await api<{ completed_today: number }>(
          `/api/analytics/my-today?area=${area}`,
        )
        if (!cancelled) setN(Number(row.completed_today) || 0)
      } catch {
        if (!cancelled) setN(null)
      }
    }
    void load()
    const onRefresh = () => {
      void load()
    }
    window.addEventListener(TODAY_REFRESH, onRefresh)
    return () => {
      cancelled = true
      window.removeEventListener(TODAY_REFRESH, onRefresh)
    }
  }, [area])

  if (n === null) return null
  return (
    <div
      className={
        compact
          ? 'flex items-baseline gap-2 rounded-lg border border-sky-200 bg-sky-50/70 px-3 py-1 dark:border-sky-900/60 dark:bg-sky-950/30'
          : 'rounded-xl border border-sky-200 bg-sky-50/70 px-4 py-2 text-right dark:border-sky-900/60 dark:bg-sky-950/30'
      }
    >
      <div className="text-[11px] font-medium uppercase tracking-wide text-sky-700 dark:text-sky-300">
        Completed today
      </div>
      <div
        className={`font-display font-semibold tabular-nums text-sky-900 dark:text-sky-100 ${
          compact ? 'text-lg' : 'text-2xl'
        }`}
      >
        {n.toLocaleString()}
      </div>
    </div>
  )
}
