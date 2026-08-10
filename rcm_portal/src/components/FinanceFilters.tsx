import { useEffect, useMemo, useState } from 'react'
import { forecastApi, monthLastDay, type Filters } from '../api/forecast'
import { Badge, Button, Card, Input } from './ui'

const MONTH_LABELS: Record<string, string> = {
  '2026-01': 'Jan',
  '2026-02': 'Feb',
  '2026-03': 'Mar',
  '2026-04': 'Apr',
  '2026-05': 'May',
  '2026-06': 'Jun',
  '2026-07': 'Jul',
  '2026-08': 'Aug',
}
const MONTH_KEYS = Object.keys(MONTH_LABELS)

function CheckList({
  options,
  value,
  onChange,
  maxHeight = 140,
}: {
  options: string[]
  value: string[]
  onChange: (v: string[]) => void
  maxHeight?: number
}) {
  const toggle = (opt: string) => {
    if (value.includes(opt)) onChange(value.filter((x) => x !== opt))
    else onChange([...value, opt])
  }
  if (!options.length) {
    return <p className="text-xs text-gray-400">No options loaded</p>
  }
  return (
    <div
      className="space-y-1 overflow-y-auto rounded-lg border border-gray-100 p-2 dark:border-gray-800"
      style={{ maxHeight }}
    >
      {options.map((o) => {
        const label = o || '(blank)'
        const key = o || '__blank__'
        return (
          <label
            key={key}
            className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-sm text-gray-700 hover:bg-gray-50 dark:text-gray-200 dark:hover:bg-gray-800"
          >
            <input
              type="checkbox"
              className="rounded border-gray-300 text-brand-600 focus:ring-brand-500"
              checked={value.includes(o)}
              onChange={() => toggle(o)}
            />
            <span className="truncate" title={label}>
              {label}
            </span>
          </label>
        )
      })}
    </div>
  )
}

export function FinanceFilters({
  filters,
  onChange,
}: {
  filters: Filters
  onChange: (next: Filters) => void
}) {
  const [opts, setOpts] = useState({
    facilities: [] as string[],
    insurers: [] as string[],
    months: [] as string[],
    date_min: '2026-01-01',
    date_max: '2026-08-31',
  })
  const [loadError, setLoadError] = useState('')

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const meta = await forecastApi.filters()
        if (cancelled) return
        setOpts({
          facilities: meta.facilities || [],
          insurers: meta.insurers || [],
          months: meta.months?.length ? meta.months : MONTH_KEYS,
          date_min: meta.date_min || '2026-01-01',
          date_max: meta.date_max || '2026-08-31',
        })
      } catch (e) {
        if (!cancelled) setLoadError(String((e as Error).message || e))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const patch = (partial: Partial<Filters>) => onChange({ ...filters, ...partial })

  const clear = () =>
    onChange({
      facility: [],
      ins: [],
      stage: [],
      month: [],
      dateFrom: '',
      dateTo: '',
      severity: [],
      riskFlag: [],
      q: '',
    })

  const toggleMonth = (ym: string) => {
    // Day range wins over month in API qs(); clear dates when picking months
    const next = filters.month.includes(ym)
      ? filters.month.filter((m) => m !== ym)
      : [...filters.month, ym]
    patch({ month: next, dateFrom: '', dateTo: '' })
  }

  const setMonthRange = (ym: string) => {
    const from = `${ym}-01`
    const to = monthLastDay(ym)
    const active =
      filters.dateFrom === from && filters.dateTo === to && filters.month.length === 0
    if (active) {
      patch({ dateFrom: '', dateTo: '', month: [] })
      return
    }
    patch({ dateFrom: from, dateTo: to, month: [] })
  }

  const chips = useMemo(() => {
    const out: string[] = []
    filters.facility.forEach((f) => out.push(`Clinic: ${f || '(blank)'}`))
    filters.ins.forEach((f) => out.push(`Ins: ${f}`))
    if (filters.dateFrom || filters.dateTo) {
      out.push(`Days: ${filters.dateFrom || '…'} → ${filters.dateTo || '…'}`)
    } else {
      filters.month.forEach((m) => out.push(`Month: ${MONTH_LABELS[m] || m}`))
    }
    return out
  }, [filters])

  const monthOptions = opts.months.length ? opts.months : MONTH_KEYS
  const hasDayRange = Boolean(filters.dateFrom || filters.dateTo)

  return (
    <Card
      title="Filters"
      action={
        <Button type="button" variant="secondary" onClick={clear}>
          Clear
        </Button>
      }
    >
      <div className="grid gap-4 lg:grid-cols-4">
        <div className="space-y-2">
          <div className="text-xs font-semibold uppercase tracking-wide text-gray-500">
            Day range
          </div>
          <label className="block text-xs text-gray-500">From</label>
          <Input
            type="date"
            value={filters.dateFrom}
            min={opts.date_min}
            max={opts.date_max}
            onChange={(e) => patch({ dateFrom: e.target.value, month: [] })}
          />
          <label className="block text-xs text-gray-500">To</label>
          <Input
            type="date"
            value={filters.dateTo}
            min={opts.date_min}
            max={opts.date_max}
            onChange={(e) => patch({ dateTo: e.target.value, month: [] })}
          />
          <p className="text-xs text-gray-400">Day range overrides month selection.</p>
        </div>

        <div className="space-y-2">
          <div className="text-xs font-semibold uppercase tracking-wide text-gray-500">
            Month {hasDayRange ? '(ignored while day range set)' : ''}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {monthOptions.map((ym) => {
              const from = `${ym}-01`
              const to = monthLastDay(ym)
              const rangeActive =
                filters.dateFrom === from && filters.dateTo === to && !filters.month.length
              const multiActive = filters.month.includes(ym) && !hasDayRange
              const active = rangeActive || multiActive
              return (
                <button
                  key={ym}
                  type="button"
                  title={`Click: set day range ${from}–${to}. Shift+click: multi-month.`}
                  className={`rounded-full px-2.5 py-1 text-xs font-semibold transition ${
                    active
                      ? 'bg-brand-600 text-white'
                      : 'bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700'
                  }`}
                  onClick={(e) => {
                    if (e.shiftKey) toggleMonth(ym)
                    else setMonthRange(ym)
                  }}
                >
                  {MONTH_LABELS[ym] || ym}
                </button>
              )
            })}
          </div>
          <p className="text-xs text-gray-400">Click = that month as days. Shift+click = multi-month.</p>
        </div>

        <div className="space-y-2">
          <div className="text-xs font-semibold uppercase tracking-wide text-gray-500">
            Clinic {filters.facility.length ? `(${filters.facility.length})` : '(all)'}
          </div>
          <CheckList
            options={opts.facilities}
            value={filters.facility}
            onChange={(facility) => patch({ facility })}
          />
        </div>

        <div className="space-y-2">
          <div className="text-xs font-semibold uppercase tracking-wide text-gray-500">
            Insurance {filters.ins.length ? `(${filters.ins.length})` : '(all)'}
          </div>
          <CheckList
            options={opts.insurers}
            value={filters.ins}
            onChange={(ins) => patch({ ins })}
            maxHeight={160}
          />
        </div>
      </div>

      {loadError && (
        <p className="mt-3 text-xs text-rose-600 dark:text-rose-300">
          Could not load filter options: {loadError}
        </p>
      )}

      {chips.length > 0 && (
        <div className="mt-4 flex flex-wrap gap-1.5">
          {chips.map((c) => (
            <Badge key={c} tone="blue">
              {c}
            </Badge>
          ))}
        </div>
      )}
    </Card>
  )
}
