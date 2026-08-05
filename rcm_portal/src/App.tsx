import { Navigate, Route, Routes } from 'react-router-dom'
import { useAuth } from './auth/AuthContext'
import { Layout } from './components/Layout'
import { LoginPage } from './pages/Login'
import { EligibilityQueuePage } from './pages/EligibilityQueue'
import { FinancePage } from './pages/Finance'
import { UsersPage } from './pages/Users'
import { PlatformPage } from './pages/Platform'
import { TransactionTrackerPage } from './pages/TransactionTracker'
import type { Role } from './api/client'

function Protected({
  children,
  roles,
  trackerView,
}: {
  children: React.ReactNode
  roles?: Role[]
  trackerView?: boolean
}) {
  const { user, loading, hasRole, canTracker } = useAuth()
  if (loading) {
    return (
      <div className="flex min-h-full items-center justify-center text-sm text-gray-500">
        Loading…
      </div>
    )
  }
  if (!user) return <Navigate to="/login" replace />
  if (trackerView && !canTracker('view')) return <Navigate to="/" replace />
  if (roles && !hasRole(...roles)) return <Navigate to="/" replace />
  return <>{children}</>
}

function HomeRedirect() {
  const { hasRole, canTracker } = useAuth()
  if (hasRole('posting_team')) return <Navigate to="/eligibility" replace />
  if (canTracker('view')) return <Navigate to="/tracker" replace />
  if (hasRole('finance')) return <Navigate to="/finance/mission" replace />
  return <Navigate to="/platform" replace />
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <Protected>
            <Layout />
          </Protected>
        }
      >
        <Route index element={<HomeRedirect />} />
        <Route
          path="/eligibility"
          element={
            <Protected roles={['posting_team', 'super_admin']}>
              <EligibilityQueuePage />
            </Protected>
          }
        />
        <Route
          path="/tracker"
          element={
            <Protected trackerView>
              <TransactionTrackerPage />
            </Protected>
          }
        />
        <Route
          path="/finance/:tab"
          element={
            <Protected roles={['finance', 'super_admin']}>
              <FinancePage />
            </Protected>
          }
        />
        <Route
          path="/users"
          element={
            <Protected roles={['super_admin']}>
              <UsersPage />
            </Protected>
          }
        />
        <Route
          path="/platform"
          element={
            <Protected roles={['super_admin']}>
              <PlatformPage />
            </Protected>
          }
        />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
