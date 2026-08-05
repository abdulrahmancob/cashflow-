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
  getToken,
  login as apiLogin,
  setToken,
  type Role,
  type User,
} from '../api/client'
import { fetchTrackerMe, type TrackerPerms } from '../api/tracker'

type AuthState = {
  user: User | null
  loading: boolean
  trackerPerms: TrackerPerms | null
  login: (username: string, password: string) => Promise<void>
  logout: () => void
  hasRole: (...roles: Role[]) => boolean
  canTracker: (perm?: 'view' | 'edit' | 'upload' | 'admin') => boolean
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [trackerPerms, setTrackerPerms] = useState<TrackerPerms | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    if (!getToken()) {
      setUser(null)
      setTrackerPerms(null)
      setLoading(false)
      return
    }
    try {
      const me = await fetchMe()
      setUser(me)
      try {
        setTrackerPerms(await fetchTrackerMe())
      } catch {
        setTrackerPerms(null)
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setToken(null)
      setUser(null)
      setTrackerPerms(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const login = useCallback(async (username: string, password: string) => {
    const u = await apiLogin(username, password)
    setUser(u)
    try {
      setTrackerPerms(await fetchTrackerMe())
    } catch {
      setTrackerPerms(null)
    }
  }, [])

  const logout = useCallback(() => {
    setToken(null)
    setUser(null)
    setTrackerPerms(null)
  }, [])

  const hasRole = useCallback(
    (...roles: Role[]) => {
      if (!user) return false
      if (user.roles.includes('super_admin')) return true
      return roles.some((r) => user.roles.includes(r))
    },
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

  const value = useMemo(
    () => ({
      user,
      loading,
      trackerPerms,
      login,
      logout,
      hasRole,
      canTracker,
      refresh,
    }),
    [user, loading, trackerPerms, login, logout, hasRole, canTracker, refresh],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth outside AuthProvider')
  return ctx
}
