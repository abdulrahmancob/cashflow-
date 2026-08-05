import { useState, type FormEvent } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { ApiError } from '../api/client'
import { Button, Input } from '../components/ui'

export function LoginPage() {
  const { user, loading, login, hasRole } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState('abdelrahman.hamdy@cobsolution.com')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  if (!loading && user) {
    if (hasRole('posting_team')) return <Navigate to="/eligibility" replace />
    if (hasRole('finance')) return <Navigate to="/finance/mission" replace />
    return <Navigate to="/platform" replace />
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      await login(username, password)
      navigate('/')
    } catch (err) {
      setError(err instanceof ApiError ? String(err.message) : 'Login failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="relative flex min-h-full items-center justify-center overflow-hidden bg-gradient-to-br from-slate-100 via-brand-50 to-sky-100 px-4 dark:from-gray-950 dark:via-slate-900 dark:to-gray-900">
      <div className="pointer-events-none absolute inset-0 opacity-40 [background:radial-gradient(circle_at_20%_20%,#93c5fd_0,transparent_35%),radial-gradient(circle_at_80%_0%,#bfdbfe_0,transparent_30%)]" />
      <form
        onSubmit={onSubmit}
        className="relative w-full max-w-md rounded-3xl border border-white/60 bg-white/90 p-8 shadow-xl backdrop-blur dark:border-gray-800 dark:bg-gray-900/90"
      >
        <div className="mb-8">
          <div className="font-display text-xs font-semibold uppercase tracking-[0.2em] text-brand-600">
            RCM Platform
          </div>
          <h1 className="font-display mt-2 text-3xl font-semibold text-gray-900 dark:text-white">
            Operations Portal
          </h1>
          <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">
            Sign in to your work queue and finance dashboards.
          </p>
        </div>
        <label className="mb-4 block text-sm font-medium text-gray-700 dark:text-gray-300">
          Username
          <Input
            className="mt-1"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            required
          />
        </label>
        <label className="mb-6 block text-sm font-medium text-gray-700 dark:text-gray-300">
          Password
          <Input
            className="mt-1"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        {error && <p className="mb-4 text-sm text-rose-600">{error}</p>}
        <Button className="w-full" disabled={busy} type="submit">
          {busy ? 'Signing in…' : 'Sign in'}
        </Button>
      </form>
    </div>
  )
}
