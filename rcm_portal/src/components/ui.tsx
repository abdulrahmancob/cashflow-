import type {
  ButtonHTMLAttributes,
  CSSProperties,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
} from 'react'
import { useEffect, useLayoutEffect, useRef, useState as useStateUI } from 'react'
import { createPortal } from 'react-dom'
import {
  AlertCircle,
  CheckCircle2,
  Info,
  TrendingDown,
  TrendingUp,
  X,
} from 'lucide-react'

const focusRing =
  'outline-none focus-visible:ring-4 focus-visible:ring-brand-500/20 focus-visible:border-brand-500'

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string
  description?: string
  actions?: ReactNode
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="font-display text-2xl font-semibold tracking-tight text-gray-900 dark:text-white">
          {title}
        </h1>
        {description && (
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">{description}</p>
        )}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

export function Card({
  children,
  className = '',
  title,
  description,
  action,
  padded = true,
}: {
  children: ReactNode
  className?: string
  title?: string
  description?: string
  action?: ReactNode
  padded?: boolean
}) {
  return (
    <div
      className={`overflow-hidden rounded-xl border border-gray-200 bg-white shadow-xs dark:border-gray-800 dark:bg-gray-900 ${className}`}
    >
      {(title || action || description) && (
        <div className="flex items-start justify-between gap-3 border-b border-gray-200 px-5 py-4 dark:border-gray-800">
          <div className="min-w-0">
            {title ? (
              <h3 className="font-display text-base font-semibold text-gray-900 dark:text-white">
                {title}
              </h3>
            ) : (
              <span />
            )}
            {description && (
              <p className="mt-0.5 text-sm text-gray-500 dark:text-gray-400">{description}</p>
            )}
          </div>
          {action}
        </div>
      )}
      <div className={padded ? 'p-5' : ''}>{children}</div>
    </div>
  )
}

export function KpiCard({
  label,
  value,
  tone = 'default',
  trend,
  trendLabel = 'vs last period',
  invertTrend = false,
  hint,
}: {
  label: string
  value: string | number
  tone?: 'default' | 'warn' | 'ok' | 'danger' | 'info'
  trend?: number
  trendLabel?: string
  /** When true, a falling number is the good direction (overdue, DSO, error). */
  invertTrend?: boolean
  hint?: string
}) {
  const tones: Record<string, string> = {
    default: 'border-gray-200 dark:border-gray-800',
    warn: 'border-amber-200/80 dark:border-amber-900/60',
    ok: 'border-emerald-200/80 dark:border-emerald-900/60',
    danger: 'border-rose-200/80 dark:border-rose-900/60',
    info: 'border-sky-200/80 dark:border-sky-900/60',
  }
  const dots: Record<string, string> = {
    default: 'bg-gray-400',
    warn: 'bg-amber-500',
    ok: 'bg-emerald-500',
    danger: 'bg-rose-500',
    info: 'bg-sky-500',
  }
  const up = trend != null && trend >= 0
  const good = trend != null && (invertTrend ? trend <= 0 : trend >= 0)
  return (
    <div
      className={`rounded-xl border bg-white p-5 shadow-xs dark:bg-gray-900 ${tones[tone]}`}
    >
      <div className="flex items-center gap-2 text-sm font-medium text-gray-500 dark:text-gray-400">
        <span className={`h-1.5 w-1.5 rounded-full ${dots[tone]}`} />
        {label}
      </div>
      <div className="font-display mt-2 text-3xl font-semibold tracking-tight text-gray-900 dark:text-white">
        {value}
      </div>
      {hint && (
        <div className="mt-1 text-xs text-gray-400">{hint}</div>
      )}
      {trend != null && Number.isFinite(trend) && (
        <div
          className={`mt-2 inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold ${
            good
              ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300'
              : 'bg-rose-50 text-rose-700 dark:bg-rose-950/50 dark:text-rose-300'
          }`}
        >
          {up ? <TrendingUp className="h-3.5 w-3.5" /> : <TrendingDown className="h-3.5 w-3.5" />}
          {up ? '+' : ''}
          {trend.toFixed(1)}%
          <span className="font-medium text-gray-400">{trendLabel}</span>
        </div>
      )}
    </div>
  )
}

export function Badge({
  children,
  tone = 'gray',
  dot = false,
  outline = false,
}: {
  children: ReactNode
  tone?: 'gray' | 'blue' | 'green' | 'amber' | 'red' | 'purple'
  dot?: boolean
  outline?: boolean
}) {
  const solid: Record<string, string> = {
    gray: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300',
    blue: 'bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300',
    green: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300',
    amber: 'bg-amber-50 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
    red: 'bg-rose-50 text-rose-700 dark:bg-rose-950 dark:text-rose-300',
    purple: 'bg-violet-50 text-violet-700 dark:bg-violet-950 dark:text-violet-300',
  }
  const outlined: Record<string, string> = {
    gray: 'border border-gray-200 bg-white text-gray-700 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-300',
    blue: 'border border-blue-200 bg-white text-blue-700 dark:border-blue-800 dark:bg-gray-900 dark:text-blue-300',
    green:
      'border border-emerald-200 bg-white text-emerald-700 dark:border-emerald-800 dark:bg-gray-900 dark:text-emerald-300',
    amber:
      'border border-amber-200 bg-white text-amber-800 dark:border-amber-800 dark:bg-gray-900 dark:text-amber-300',
    red: 'border border-rose-200 bg-white text-rose-700 dark:border-rose-800 dark:bg-gray-900 dark:text-rose-300',
    purple:
      'border border-violet-200 bg-white text-violet-700 dark:border-violet-800 dark:bg-gray-900 dark:text-violet-300',
  }
  const dots: Record<string, string> = {
    gray: 'bg-gray-500',
    blue: 'bg-blue-500',
    green: 'bg-emerald-500',
    amber: 'bg-amber-500',
    red: 'bg-rose-500',
    purple: 'bg-violet-500',
  }
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${
        outline ? outlined[tone] : solid[tone]
      }`}
    >
      {dot && <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dots[tone]}`} />}
      {children}
    </span>
  )
}

export function Button({
  variant = 'primary',
  size = 'md',
  className = '',
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
  size?: 'sm' | 'md' | 'lg' | 'icon'
}) {
  const styles = {
    primary:
      'bg-brand-600 text-white shadow-xs hover:bg-brand-700 disabled:bg-brand-600/50',
    secondary:
      'bg-white text-gray-700 shadow-xs ring-1 ring-gray-200 hover:bg-gray-50 dark:bg-gray-900 dark:text-gray-100 dark:ring-gray-700 dark:hover:bg-gray-800',
    ghost: 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800',
    danger: 'bg-rose-600 text-white shadow-xs hover:bg-rose-700',
  }
  const sizes = {
    sm: 'h-8 rounded-lg px-3 text-sm',
    md: 'h-10 rounded-lg px-3.5 text-sm',
    lg: 'h-11 rounded-lg px-4 text-sm',
    icon: 'h-10 w-10 rounded-lg p-0',
  }
  return (
    <button
      className={`inline-flex items-center justify-center gap-2 font-semibold transition disabled:cursor-not-allowed ${styles[variant]} ${sizes[size]} ${className}`}
      {...props}
    >
      {children}
    </button>
  )
}

export function Input({
  icon,
  className = '',
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { icon?: ReactNode }) {
  return (
    <div className={`relative ${className}`}>
      {icon && (
        <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-gray-400">
          {icon}
        </span>
      )}
      <input
        {...props}
        className={`h-10 w-full rounded-lg border border-gray-300 bg-white px-3 text-sm text-gray-900 shadow-xs placeholder:text-gray-400 ${focusRing} dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100 ${
          icon ? 'pl-9' : ''
        }`}
      />
    </div>
  )
}

export function Select({
  className = '',
  ...props
}: SelectHTMLAttributes<HTMLSelectElement>) {
  const width = /(?:^|\s)w-/.test(className) ? '' : 'w-full'
  return (
    <select
      {...props}
      className={`h-10 max-w-full rounded-lg border border-gray-300 bg-white px-3 text-sm text-gray-900 shadow-xs ${focusRing} dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100 ${width} ${className}`}
    />
  )
}

type DropdownPos = {
  top?: number
  bottom?: number
  left: number
  width: number
  maxHeight: number
}

function dropdownPos(btn: HTMLElement): DropdownPos {
  const r = btn.getBoundingClientRect()
  const gap = 4
  const minWidth = Math.max(r.width, 220)
  const pad = 8
  const spaceBelow = window.innerHeight - r.bottom - gap - pad
  const spaceAbove = r.top - gap - pad
  const preferBelow = spaceBelow >= 180 || spaceBelow >= spaceAbove
  const maxHeight = Math.max(180, Math.min(320, preferBelow ? spaceBelow : spaceAbove))
  const width = Math.min(minWidth, window.innerWidth - pad * 2)
  const left = Math.min(Math.max(pad, r.left), window.innerWidth - width - pad)
  return preferBelow
    ? { top: r.bottom + gap, left, width, maxHeight }
    : { bottom: window.innerHeight - r.top + gap, left, width, maxHeight }
}

type MultiOption = {
  label: string
  value: string
  children?: { label: string; value: string }[]
}

function optionLabel(options: MultiOption[], selected: string) {
  for (const opt of options) {
    if (opt.value === selected && !opt.children?.length) return opt.label
    const child = opt.children?.find((item) => item.value === selected)
    if (child) return child.label
  }
  return selected
}

function IndeterminateCheckbox({
  checked,
  indeterminate,
  onChange,
}: {
  checked: boolean
  indeterminate: boolean
  onChange: () => void
}) {
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate
  }, [indeterminate])
  return (
    <input
      ref={ref}
      type="checkbox"
      className="rounded border-gray-300 text-brand-600"
      checked={checked}
      onChange={onChange}
    />
  )
}

export function MultiSelect({
  options,
  value,
  onChange,
  placeholder = 'All',
  className = '',
}: {
  options: MultiOption[]
  value: string[]
  onChange: (v: string[]) => void
  placeholder?: string
  className?: string
}) {
  const [open, setOpen] = useStateUI(false)
  const [q, setQ] = useStateUI('')
  const [pos, setPos] = useStateUI<DropdownPos | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const needle = q.trim().toLowerCase()
  const filtered = needle
    ? options.flatMap((opt) => {
        if (!opt.children?.length) {
          return opt.label.toLowerCase().includes(needle) ? [opt] : []
        }
        const parentHit = opt.label.toLowerCase().includes(needle)
        const children = parentHit
          ? opt.children
          : opt.children.filter((child) => child.label.toLowerCase().includes(needle))
        return parentHit || children.length ? [{ ...opt, children }] : []
      })
    : options

  function toggle(v: string) {
    onChange(value.includes(v) ? value.filter((x) => x !== v) : [...value, v])
  }

  function toggleMany(ids: string[]) {
    const allOn = ids.length > 0 && ids.every((id) => value.includes(id))
    if (allOn) onChange(value.filter((item) => !ids.includes(item)))
    else onChange([...value.filter((item) => !ids.includes(item)), ...ids])
  }

  useEffect(() => {
    if (!open) setQ('')
  }, [open])

  useLayoutEffect(() => {
    if (!open) {
      setPos(null)
      return
    }
    const place = () => {
      if (buttonRef.current) setPos(dropdownPos(buttonRef.current))
    }
    place()
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node
      if (rootRef.current?.contains(t) || menuRef.current?.contains(t)) return
      setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const summary =
    value.length === 0
      ? placeholder
      : value.length === 1
        ? optionLabel(options, value[0])
        : `${value.length} selected`

  const menu =
    open && pos
      ? createPortal(
          <div
            ref={menuRef}
            style={{
              position: 'fixed',
              top: pos.top,
              bottom: pos.bottom,
              left: pos.left,
              width: pos.width,
              maxHeight: pos.maxHeight,
            }}
            className="z-[80] flex flex-col overflow-hidden rounded-lg border border-gray-200 bg-white shadow-lg dark:border-gray-700 dark:bg-gray-950"
          >
            <div className="shrink-0 px-2 pb-1 pt-2">
              <input
                autoFocus
                value={q}
                placeholder="Search…"
                className="h-8 w-full rounded border border-gray-200 bg-white px-2 text-sm dark:border-gray-700 dark:bg-gray-950"
                onChange={(e) => setQ(e.target.value)}
              />
            </div>
            {value.length > 0 && (
              <button
                type="button"
                className="shrink-0 w-full px-3 py-1.5 text-left text-xs font-medium text-brand-600 hover:bg-gray-50 dark:hover:bg-gray-800"
                onClick={() => { onChange([]); setOpen(false) }}
              >
                Clear all
              </button>
            )}
            <div className="min-h-0 flex-1 overflow-y-auto py-1">
              {filtered.map((opt) => {
                if (opt.children?.length) {
                  const full =
                    options.find((item) => item.value === opt.value)?.children ?? opt.children
                  const ids = full.map((child) => child.value)
                  const selectedCount = ids.filter((id) => value.includes(id)).length
                  const allOn = selectedCount === ids.length && ids.length > 0
                  return (
                    <div key={opt.value}>
                      <label className="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-sm font-medium hover:bg-gray-50 dark:hover:bg-gray-800">
                        <IndeterminateCheckbox
                          checked={allOn}
                          indeterminate={selectedCount > 0 && !allOn}
                          onChange={() => toggleMany(ids)}
                        />
                        <span className="text-gray-900 dark:text-gray-100">{opt.label}</span>
                      </label>
                      {opt.children.map((child) => (
                        <label
                          key={child.value}
                          className="flex cursor-pointer items-center gap-2 py-1.5 pl-8 pr-3 text-sm hover:bg-gray-50 dark:hover:bg-gray-800"
                        >
                          <input
                            type="checkbox"
                            className="rounded border-gray-300 text-brand-600"
                            checked={value.includes(child.value)}
                            onChange={() => toggle(child.value)}
                          />
                          <span className="text-gray-900 dark:text-gray-100">{child.label}</span>
                        </label>
                      ))}
                    </div>
                  )
                }
                return (
                  <label
                    key={opt.value}
                    className="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-sm hover:bg-gray-50 dark:hover:bg-gray-800"
                  >
                    <input
                      type="checkbox"
                      className="rounded border-gray-300 text-brand-600"
                      checked={value.includes(opt.value)}
                      onChange={() => toggle(opt.value)}
                    />
                    <span className="text-gray-900 dark:text-gray-100">{opt.label}</span>
                  </label>
                )
              })}
              {!filtered.length ? (
                <div className="px-3 py-1.5 text-sm text-gray-400">No matches</div>
              ) : null}
            </div>
          </div>,
          document.body,
        )
      : null

  return (
    <div ref={rootRef} className={`relative ${className}`}>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={`h-10 max-w-full rounded-lg border border-gray-300 bg-white px-3 text-left text-sm shadow-xs ${focusRing} dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100 w-full flex items-center justify-between gap-1 ${value.length > 0 ? 'text-gray-900 dark:text-gray-100' : 'text-gray-400 dark:text-gray-500'}`}
      >
        <span className="truncate">{summary}</span>
        <svg className="h-4 w-4 shrink-0 text-gray-400" viewBox="0 0 20 20" fill="currentColor">
          <path fillRule="evenodd" d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z" clipRule="evenodd" />
        </svg>
      </button>
      {menu}
    </div>
  )
}

export function SearchableSelect({
  options,
  value,
  onChange,
  placeholder = 'Select…',
  className = '',
  disabled = false,
  style,
}: {
  options: { label: string; value: string }[]
  value: string
  onChange: (v: string) => void
  placeholder?: string
  className?: string
  disabled?: boolean
  style?: CSSProperties
}) {
  const [open, setOpen] = useStateUI(false)
  const [q, setQ] = useStateUI('')
  const [pos, setPos] = useStateUI<DropdownPos | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const filtered = q.trim()
    ? options.filter((o) => o.label.toLowerCase().includes(q.trim().toLowerCase()))
    : options
  const selected = options.find((o) => o.value === value)

  useEffect(() => {
    if (!open) setQ('')
  }, [open])

  useLayoutEffect(() => {
    if (!open) {
      setPos(null)
      return
    }
    const place = () => {
      if (buttonRef.current) setPos(dropdownPos(buttonRef.current))
    }
    place()
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      const t = e.target as Node
      if (rootRef.current?.contains(t) || menuRef.current?.contains(t)) return
      setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const menu =
    open && !disabled && pos
      ? createPortal(
          <div
            ref={menuRef}
            style={{
              position: 'fixed',
              top: pos.top,
              bottom: pos.bottom,
              left: pos.left,
              width: pos.width,
              maxHeight: pos.maxHeight,
            }}
            className="z-[80] flex flex-col overflow-hidden rounded-lg border border-gray-200 bg-white shadow-lg dark:border-gray-700 dark:bg-gray-950"
          >
            <div className="shrink-0 px-2 pb-1 pt-2">
              <input
                autoFocus
                value={q}
                placeholder="Search…"
                className="h-8 w-full rounded border border-gray-200 bg-white px-2 text-sm dark:border-gray-700 dark:bg-gray-950"
                onChange={(e) => setQ(e.target.value)}
              />
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto py-1">
              {filtered.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  className={`w-full px-3 py-1.5 text-left text-sm hover:bg-gray-50 dark:hover:bg-gray-800 ${opt.value === value ? 'font-medium text-brand-600' : 'text-gray-900 dark:text-gray-100'}`}
                  onClick={() => {
                    onChange(opt.value)
                    setOpen(false)
                    setQ('')
                  }}
                >
                  {opt.label}
                </button>
              ))}
              {!filtered.length ? (
                <div className="px-3 py-1.5 text-sm text-gray-400">No matches</div>
              ) : null}
            </div>
          </div>,
          document.body,
        )
      : null

  return (
    <div ref={rootRef} className={`relative ${className}`}>
      <button
        ref={buttonRef}
        type="button"
        disabled={disabled}
        style={style}
        onClick={() => setOpen((o) => !o)}
        className={`h-10 max-w-full rounded-lg border border-gray-300 bg-white px-3 text-left text-sm shadow-xs ${focusRing} dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100 w-full flex items-center justify-between gap-1 disabled:opacity-50 ${selected ? 'text-gray-900 dark:text-gray-100' : 'text-gray-400 dark:text-gray-500'}`}
      >
        <span className="truncate">{selected?.label ?? placeholder}</span>
        <svg className="h-4 w-4 shrink-0 text-gray-400" viewBox="0 0 20 20" fill="currentColor">
          <path fillRule="evenodd" d="M5.293 7.293a1 1 0 011.414 0L10 10.586l3.293-3.293a1 1 0 111.414 1.414l-4 4a1 1 0 01-1.414 0l-4-4a1 1 0 010-1.414z" clipRule="evenodd" />
        </svg>
      </button>
      {menu}
    </div>
  )
}

export function TextArea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      {...props}
      className={`w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 shadow-xs ${focusRing} dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100 ${props.className || ''}`}
    />
  )
}

export function Field({
  label,
  children,
  className = '',
}: {
  label: string
  children: ReactNode
  className?: string
}) {
  return (
    <label className={`block text-sm ${className}`}>
      <span className="mb-1.5 block font-medium text-gray-700 dark:text-gray-300">{label}</span>
      {children}
    </label>
  )
}

export function Drawer({
  open,
  onClose,
  title,
  children,
  wide,
}: {
  open: boolean
  onClose: () => void
  title: string
  children: ReactNode
  wide?: boolean
}) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <button
        className="absolute inset-0 bg-gray-900/40 backdrop-blur-[2px]"
        aria-label="Close drawer"
        onClick={onClose}
      />
      <aside
        className={`relative flex h-full w-full flex-col border-l border-gray-200 bg-white shadow-2xl dark:border-gray-800 dark:bg-gray-900 ${
          wide ? 'max-w-4xl' : 'max-w-xl'
        }`}
      >
        <header className="flex items-center justify-between border-b border-gray-200 px-5 py-4 dark:border-gray-800">
          <h2 className="font-display text-lg font-semibold text-gray-900 dark:text-white">
            {title}
          </h2>
          <Button variant="ghost" size="icon" onClick={onClose} type="button" aria-label="Close">
            <X className="h-4 w-4" />
          </Button>
        </header>
        <div className="flex-1 overflow-y-auto p-5">{children}</div>
      </aside>
    </div>
  )
}

export function Toast({
  message,
  tone = 'info',
  onDismiss,
}: {
  message: string
  tone?: 'info' | 'error' | 'success'
  onDismiss?: () => void
}) {
  const tones = {
    info: 'border-brand-200 bg-white text-gray-800 dark:border-brand-800 dark:bg-gray-900 dark:text-gray-100',
    error: 'border-rose-200 bg-white text-gray-800 dark:border-rose-800 dark:bg-gray-900 dark:text-gray-100',
    success:
      'border-emerald-200 bg-white text-gray-800 dark:border-emerald-800 dark:bg-gray-900 dark:text-gray-100',
  }
  const icons = {
    info: <Info className="h-5 w-5 text-brand-600" />,
    error: <AlertCircle className="h-5 w-5 text-rose-600" />,
    success: <CheckCircle2 className="h-5 w-5 text-emerald-600" />,
  }
  return (
    <div
      className={`fixed right-4 top-4 z-[60] flex max-w-sm items-start gap-3 rounded-xl border px-4 py-3 shadow-lg ${tones[tone]}`}
    >
      {icons[tone]}
      <p className="flex-1 text-sm font-medium">{message}</p>
      {onDismiss && (
        <button
          className="rounded-md p-0.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800"
          onClick={onDismiss}
          type="button"
          aria-label="Dismiss"
        >
          <X className="h-4 w-4" />
        </button>
      )}
    </div>
  )
}

export function Alert({
  children,
  tone = 'error',
}: {
  children: ReactNode
  tone?: 'error' | 'warning' | 'info'
}) {
  const map = {
    error:
      'border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-200',
    warning:
      'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200',
    info: 'border-brand-200 bg-brand-50 text-brand-800 dark:border-brand-900 dark:bg-brand-950/40 dark:text-brand-200',
  }
  return (
    <div className={`flex items-start gap-2 rounded-xl border px-4 py-3 text-sm ${map[tone]}`}>
      {tone === 'error' ? (
        <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
      ) : (
        <Info className="mt-0.5 h-4 w-4 shrink-0" />
      )}
      <div>{children}</div>
    </div>
  )
}

export function Skeleton({ className = '' }: { className?: string }) {
  return (
    <div
      className={`animate-pulse rounded-md bg-gray-100 dark:bg-gray-800 ${className}`}
    />
  )
}

export function statusTone(status: string): 'gray' | 'blue' | 'green' | 'amber' | 'red' | 'purple' {
  switch (status) {
    case 'pending':
      return 'gray'
    case 'checking':
      return 'blue'
    case 'waiting_patient':
      return 'amber'
    case 'waiting_insurance':
      return 'purple'
    case 'completed':
    case 'success':
    case 'healthy':
    case 'paid':
      return 'green'
    case 'rejected':
    case 'denied':
    case 'error':
      return 'red'
    case 'deduct':
    case 'partial':
      return 'amber'
    default:
      return 'gray'
  }
}

export const chartGrid = { strokeDasharray: '3 3', stroke: '#e4e7ec', vertical: false }
export const chartAxis = { tick: { fill: '#667085', fontSize: 12 }, axisLine: false, tickLine: false }

export function ChartTooltipBox({
  active,
  payload,
  label,
}: {
  active?: boolean
  // Recharts payload is loosely typed across chart kinds.
  payload?: Array<{ name?: string; value?: number | string; color?: string; dataKey?: string }>
  label?: string
}) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-lg dark:border-gray-700 dark:bg-gray-900">
      {label && <div className="mb-1 text-xs font-medium text-gray-500">{label}</div>}
      {payload.map((p, i) => (
        <div key={p.dataKey || p.name || i} className="flex items-center gap-2 text-sm">
          <span className="h-2 w-2 rounded-full" style={{ background: p.color }} />
          <span className="text-gray-500">{p.name || p.dataKey}</span>
          <span className="font-semibold text-gray-900 dark:text-white">{p.value}</span>
        </div>
      ))}
    </div>
  )
}
