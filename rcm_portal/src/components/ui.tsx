import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from 'react'

export function Card({
  children,
  className = '',
  title,
  action,
}: {
  children: ReactNode
  className?: string
  title?: string
  action?: ReactNode
}) {
  return (
    <div
      className={`rounded-2xl border border-gray-200 bg-white shadow-sm dark:border-gray-800 dark:bg-gray-900 ${className}`}
    >
      {(title || action) && (
        <div className="flex items-center justify-between border-b border-gray-100 px-5 py-4 dark:border-gray-800">
          {title ? (
            <h3 className="font-display text-base font-semibold text-gray-900 dark:text-white">
              {title}
            </h3>
          ) : (
            <span />
          )}
          {action}
        </div>
      )}
      <div className="p-5">{children}</div>
    </div>
  )
}

export function KpiCard({
  label,
  value,
  tone = 'default',
}: {
  label: string
  value: string | number
  tone?: 'default' | 'warn' | 'ok' | 'danger' | 'info'
}) {
  const tones: Record<string, string> = {
    default: 'from-brand-50 to-white dark:from-gray-800 dark:to-gray-900',
    warn: 'from-amber-50 to-white dark:from-amber-950/40 dark:to-gray-900',
    ok: 'from-emerald-50 to-white dark:from-emerald-950/40 dark:to-gray-900',
    danger: 'from-rose-50 to-white dark:from-rose-950/40 dark:to-gray-900',
    info: 'from-sky-50 to-white dark:from-sky-950/40 dark:to-gray-900',
  }
  return (
    <div
      className={`rounded-2xl border border-gray-200 bg-gradient-to-br p-5 shadow-sm dark:border-gray-800 ${tones[tone]}`}
    >
      <div className="text-sm font-medium text-gray-500 dark:text-gray-400">{label}</div>
      <div className="font-display mt-2 text-3xl font-semibold tracking-tight text-gray-900 dark:text-white">
        {value}
      </div>
    </div>
  )
}

export function Badge({
  children,
  tone = 'gray',
}: {
  children: ReactNode
  tone?: 'gray' | 'blue' | 'green' | 'amber' | 'red' | 'purple'
}) {
  const map: Record<string, string> = {
    gray: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300',
    blue: 'bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300',
    green: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300',
    amber: 'bg-amber-50 text-amber-800 dark:bg-amber-950 dark:text-amber-300',
    red: 'bg-rose-50 text-rose-700 dark:bg-rose-950 dark:text-rose-300',
    purple: 'bg-violet-50 text-violet-700 dark:bg-violet-950 dark:text-violet-300',
  }
  return (
    <span
      className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-semibold ${map[tone]}`}
    >
      {children}
    </span>
  )
}

export function Button({
  variant = 'primary',
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
}) {
  const styles = {
    primary:
      'bg-brand-600 text-white hover:bg-brand-700 disabled:bg-brand-600/50 shadow-sm',
    secondary:
      'bg-white text-gray-800 border border-gray-200 hover:bg-gray-50 dark:bg-gray-900 dark:text-gray-100 dark:border-gray-700 dark:hover:bg-gray-800',
    ghost: 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800',
    danger: 'bg-rose-600 text-white hover:bg-rose-700',
  }
  return (
    <button
      className={`inline-flex items-center justify-center gap-2 rounded-lg px-3.5 py-2 text-sm font-semibold transition disabled:cursor-not-allowed ${styles[variant]} ${className}`}
      {...props}
    />
  )
}

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={`w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 outline-none ring-brand-500/30 focus:ring-2 dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100 ${props.className || ''}`}
    />
  )
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      {...props}
      className={`w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 outline-none ring-brand-500/30 focus:ring-2 dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100 ${props.className || ''}`}
    />
  )
}

export function TextArea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      {...props}
      className={`w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 outline-none ring-brand-500/30 focus:ring-2 dark:border-gray-700 dark:bg-gray-950 dark:text-gray-100 ${props.className || ''}`}
    />
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
        className="absolute inset-0 bg-gray-900/40 backdrop-blur-[1px]"
        aria-label="Close drawer"
        onClick={onClose}
      />
      <aside
        className={`relative flex h-full w-full flex-col border-l border-gray-200 bg-white shadow-2xl dark:border-gray-800 dark:bg-gray-900 ${
          wide ? 'max-w-2xl' : 'max-w-xl'
        }`}
      >
        <header className="flex items-center justify-between border-b border-gray-100 px-5 py-4 dark:border-gray-800">
          <h2 className="font-display text-lg font-semibold text-gray-900 dark:text-white">
            {title}
          </h2>
          <Button variant="ghost" onClick={onClose} type="button">
            Close
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
    info: 'border-brand-500 bg-brand-50 text-brand-700 dark:bg-blue-950 dark:text-blue-200',
    error: 'border-rose-500 bg-rose-50 text-rose-800 dark:bg-rose-950 dark:text-rose-200',
    success:
      'border-emerald-500 bg-emerald-50 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200',
  }
  return (
    <div
      className={`fixed right-4 top-4 z-[60] max-w-sm rounded-xl border-l-4 px-4 py-3 shadow-lg ${tones[tone]}`}
    >
      <div className="flex items-start gap-3">
        <p className="text-sm font-medium">{message}</p>
        {onDismiss && (
          <button className="text-xs opacity-70" onClick={onDismiss} type="button">
            ✕
          </button>
        )}
      </div>
    </div>
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
      return 'green'
    case 'rejected':
      return 'red'
    default:
      return 'gray'
  }
}
