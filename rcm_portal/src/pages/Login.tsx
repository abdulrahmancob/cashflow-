import { useState, type FormEvent } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { Eye, EyeOff } from 'lucide-react'
import { useAuth } from '../auth/AuthContext'
import { ApiError } from '../api/client'
import { RemitarcMark } from '../components/RemitarcMark'
import { Button, Input } from '../components/ui'

export function LoginPage() {
  const { user, loading, login } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [rememberMe, setRememberMe] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  if (loading) {
    return (
      <div className="flex min-h-full items-center justify-center bg-gray-50 dark:bg-gray-950">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-gray-200 border-t-brand-600" />
      </div>
    )
  }

  if (user) return <Navigate to="/" replace />

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    const name = username.trim()
    if (!name || !password.trim()) {
      setError('Enter your email and password')
      return
    }
    setBusy(true)
    setError('')
    try {
      await login(name, password, rememberMe)
      navigate('/')
    } catch (err) {
      setError(err instanceof ApiError ? String(err.message) : 'Login failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="relative flex min-h-full items-center justify-center bg-gray-50 px-4 dark:bg-gray-950">
      <div className="pointer-events-none absolute inset-0 [background:radial-gradient(ellipse_at_top,#dbeafe_0,transparent_45%)] dark:[background:radial-gradient(ellipse_at_top,#1e3a8a33_0,transparent_45%)]" />
      <form
        noValidate
        onSubmit={onSubmit}
        className="relative w-full max-w-[400px] rounded-xl border border-gray-200 bg-white p-8 shadow-sm dark:border-gray-800 dark:bg-gray-900"
      >
        <div className="mb-8">
          <div className="mb-5 flex items-center gap-2.5">
            <RemitarcMark />
            <div className="font-display text-sm font-semibold text-gray-900 dark:text-white">Remitarc</div>
          </div>
          <h1 className="font-display text-2xl font-semibold tracking-tight text-gray-900 dark:text-white">
            Sign in
          </h1>
          <p className="mt-1.5 text-sm text-gray-500 dark:text-gray-400">The remit, before it posts.</p>
        </div>
        <label className="mb-4 block text-sm font-medium text-gray-700 dark:text-gray-300">
          Email
          <Input
            className="mt-1.5"
            type="text"
            inputMode="email"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            spellCheck={false}
            placeholder="you@example.com"
          />
        </label>
        <label className="mb-4 block text-sm font-medium text-gray-700 dark:text-gray-300">
          Password
          <div className="relative mt-1.5">
            <Input
              type={showPassword ? 'text' : 'password'}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
            />
            <button
              type="button"
              className="absolute inset-y-0 right-0 flex items-center px-3 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
              onClick={() => setShowPassword((v) => !v)}
              aria-label={showPassword ? 'Hide password' : 'Show password'}
            >
              {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
            </button>
          </div>
        </label>
        <label className="mb-6 flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400">
          <input
            type="checkbox"
            className="h-4 w-4 rounded border-gray-300 text-brand-600 focus-visible:ring-brand-500/20"
            checked={rememberMe}
            onChange={(e) => setRememberMe(e.target.checked)}
          />
          Remember me for 12 hours
        </label>
        {error && (
          <p className="mb-4 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-300">
            {error}
          </p>
        )}
        <Button className="w-full" disabled={busy} type="submit">
          {busy ? 'Signing in…' : 'Sign in'}
        </Button>
      </form>
    </div>
  )
}
