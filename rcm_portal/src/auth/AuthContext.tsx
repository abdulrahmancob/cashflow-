import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import {
  ApiError,
  fetchMe,
  login as apiLogin,
  logout as apiLogout,
  setUnauthorizedHandler,
  type Role,
  type User,
} from '../api/client'
import { fetchTrackerMe, type TrackerPerms } from '../api/tracker'
import { announceTabClosed } from './useActivityHeartbeat'
import { fetchChecksMe, type ChecksPerms } from '../api/checksDeposits'

type AuthState = {
  user: User | null
  loading: boolean
  trackerPerms: TrackerPerms | null
  checksPerms: ChecksPerms | null
  login: (username: string, password: string, rememberMe?: boolean) => Promise<void>
  logout: () => Promise<void>
  hasRole: (...roles: Role[]) => boolean
  /** PIU sees the work pages but cannot change anything; the server rejects its writes. */
  viewOnly: boolean
  canTracker: (perm?: 'view' | 'edit' | 'upload' | 'admin') => boolean
  canChecks: (perm?: 'view' | 'edit' | 'upload' | 'admin') => boolean
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

const VIEW_ONLY_ROLES: string[] = [
  'piu',
  'client_success',
  'product_owner',
  'desk',
  'red_agent',
  'analytics_viewer',
]

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [trackerPerms, setTrackerPerms] = useState<TrackerPerms | null>(null)
  const [checksPerms, setChecksPerms] = useState<ChecksPerms | null>(null)
  const [loading, setLoading] = useState(true)

  const clearSession = useCallback(() => {
    setUser(null)
    setTrackerPerms(null)
    setChecksPerms(null)
  }, [])

  useEffect(() => {
    setUnauthorizedHandler(clearSession)
    return () => setUnauthorizedHandler(null)
  }, [clearSession])

  const refresh = useCallback(async () => {
    try {
      const me = await fetchMe()
      setUser(me)
      try {
        setTrackerPerms(await fetchTrackerMe())
      } catch {
        setTrackerPerms(null)
      }
      try {
        setChecksPerms(await fetchChecksMe())
      } catch {
        setChecksPerms(null)
      }
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) {
        /* keep going; treat as signed out */
      }
      setUser(null)
      setTrackerPerms(null)
      setChecksPerms(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const login = useCallback(async (username: string, password: string, rememberMe = false) => {
    const u = await apiLogin(username, password, rememberMe)
    setUser(u)
    try {
      setTrackerPerms(await fetchTrackerMe())
    } catch {
      setTrackerPerms(null)
    }
    try {
      setChecksPerms(await fetchChecksMe())
    } catch {
      setChecksPerms(null)
    }
  }, [])

  const logout = useCallback(async () => {
    announceTabClosed()
    await apiLogout()
    clearSession()
  }, [clearSession])

  const hasRole = useCallback(
    (...roles: Role[]) => {
      if (!user) return false
      if (user.roles.includes('super_admin')) return true
      return roles.some((r) => user.roles.includes(r))
    },
    [user],
  )

  const viewOnly = useMemo(
    () =>
      !!user &&
      user.roles.includes('piu' as Role) &&
      user.roles.every((role) => VIEW_ONLY_ROLES.includes(role)),
    [user],
  )

  const canTracker = useCallback(
    (perm: 'view' | 'edit' | 'upload' | 'admin' = 'view') => {
      if (!user) return false
      if (user.roles.includes('super_admin')) return true
      if (!trackerPerms) return false
      const map = {
        view: trackerPerms.can_view,
        edit: trackerPerms.can_edit,
        upload: trackerPerms.can_upload,
        admin: trackerPerms.can_admin,
      } as const
      return Boolean(map[perm])
    },
    [user, trackerPerms],
  )

  const canChecks = useCallback(
    (perm: 'view' | 'edit' | 'upload' | 'admin' = 'view') => {
      if (!user) return false
      if (user.roles.includes('super_admin')) return true
      if (!checksPerms) return false
      const map = {
        view: checksPerms.can_view,
        edit: checksPerms.can_edit,
        upload: checksPerms.can_upload,
        admin: checksPerms.can_admin,
      } as const
      return Boolean(map[perm])
    },
    [user, checksPerms],
  )

  const value = useMemo(
    () => ({
      user,
      loading,
      trackerPerms,
      checksPerms,
      login,
      logout,
      hasRole,
      viewOnly,
      canTracker,
      canChecks,
      refresh,
    }),
    [
      user,
      loading,
      trackerPerms,
      checksPerms,
      login,
      logout,
      hasRole,
      viewOnly,
      canTracker,
      canChecks,
      refresh,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth outside AuthProvider')
  return ctx
}
