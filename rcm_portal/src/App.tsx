import { Navigate, Route, Routes } from 'react-router-dom'
import { useAuth } from './auth/AuthContext'
import { Layout } from './components/Layout'
import { LoginPage } from './pages/Login'
import { CollectionPage } from './pages/Collection'
import { EligibilityQueuePage } from './pages/EligibilityQueue'
import { PatientResponsibilityPage } from './pages/PatientResponsibility'
import { SecondSubmissionPage } from './pages/SecondSubmission'
import { CptGuidePage } from './pages/CptGuide'
import { CptAuditQueuePage } from './pages/CptAuditQueue'
import { FinancePage } from './pages/Finance'
import { UnbankedCashPage } from './pages/UnbankedCash'
import { UsersPage } from './pages/Users'
import { ActivityPage } from './pages/Activity'
import { PlatformPage } from './pages/Platform'
import { DatabaseBrowserPage } from './pages/DatabaseBrowser'
import { TransactionTrackerPage } from './pages/TransactionTracker'
import { ChecksDepositsPage } from './pages/ChecksDeposits'
import { TeamAnalyticsPage } from './pages/TeamAnalytics'
import { AwayBoardPage } from './pages/AwayBoard'
import { BillingAnalysisPage } from './pages/BillingAnalysis'
import type { Role } from './api/client'

function Protected({
  children,
  roles,
  trackerView,
  checksView,
}: {
  children: React.ReactNode
  roles?: Role[]
  trackerView?: boolean
  checksView?: boolean
}) {
  const { user, loading, hasRole, canTracker, canChecks } = useAuth()
  if (loading) {
    return (
      <div className="flex min-h-full items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-gray-200 border-t-brand-600" />
      </div>
    )
  }
  if (!user) return <Navigate to="/login" replace />
  if (trackerView && !canTracker('view')) return <Navigate to="/" replace />
  if (checksView && !canChecks('view')) return <Navigate to="/" replace />
  if (roles && !hasRole(...roles)) return <Navigate to="/" replace />
  return <>{children}</>
}

function HomeRedirect() {
  const { hasRole, canTracker } = useAuth()
  if (hasRole('posting_team', 'collector', 'ops_admin', 'sub_admin')) return <Navigate to="/eligibility" replace />
  if (canTracker('view')) return <Navigate to="/tracker" replace />
  if (hasRole('finance')) return <Navigate to="/finance/cash" replace />
  if (hasRole('second_submission', 'second_submission_lead')) return <Navigate to="/second-submission" replace />
  if (hasRole('submission')) return <Navigate to="/cpt-guide" replace />
  if (hasRole('analytics_viewer')) return <Navigate to="/analytics" replace />
  if (hasRole('medical_audit')) return <Navigate to="/cpt-audit" replace />
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
            <Protected roles={['posting_team', 'super_admin', 'sub_admin', 'collector', 'finance', 'ops_admin']}>
              <EligibilityQueuePage />
            </Protected>
          }
        />
        <Route
          path="/second-submission"
          element={
            <Protected roles={['second_submission', 'second_submission_lead', 'super_admin', 'sub_admin', 'ops_admin']}>
              <SecondSubmissionPage />
            </Protected>
          }
        />
        <Route
          path="/patient-responsibility"
          element={
            <Protected
              roles={[
                'posting_team',
                'second_submission',
                'second_submission_lead',
                'super_admin',
                'sub_admin',
                'ops_admin',
                'finance',
              ]}
            >
              <PatientResponsibilityPage />
            </Protected>
          }
        />
        <Route
          path="/collection"
          element={
            <Protected roles={['posting_team', 'super_admin', 'sub_admin', 'collector', 'finance', 'ops_admin']}>
              <CollectionPage />
            </Protected>
          }
        />
        <Route
          path="/analytics"
          element={
            <Protected roles={['super_admin', 'sub_admin', 'ops_admin', 'second_submission_lead', 'analytics_viewer']}>
              <TeamAnalyticsPage />
            </Protected>
          }
        />
        <Route
          path="/billing-analysis"
          element={
            <Protected roles={['super_admin', 'sub_admin', 'ops_admin']}>
              <BillingAnalysisPage />
            </Protected>
          }
        />
        <Route
          path="/cpt-guide"
          element={
            <Protected roles={['posting_team', 'super_admin', 'sub_admin', 'finance', 'submission', 'ops_admin']}>
              <CptGuidePage />
            </Protected>
          }
        />
        <Route
          path="/cpt-audit"
          element={
            <Protected roles={['medical_audit', 'super_admin', 'sub_admin', 'ops_admin']}>
              <CptAuditQueuePage />
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
          path="/checks-deposits"
          element={
            <Protected checksView>
              <ChecksDepositsPage />
            </Protected>
          }
        />
        <Route
          path="/finance/unbanked"
          element={
            <Protected roles={['finance', 'super_admin', 'sub_admin']}>
              <UnbankedCashPage />
            </Protected>
          }
        />
        <Route
          path="/finance/:tab"
          element={
            <Protected roles={['finance', 'super_admin', 'sub_admin']}>
              <FinancePage />
            </Protected>
          }
        />
        <Route
          path="/away"
          element={
            <Protected roles={['super_admin', 'sub_admin', 'ops_admin', 'second_submission_lead']}>
              <AwayBoardPage />
            </Protected>
          }
        />
        <Route
          path="/activity"
          element={
            <Protected roles={['super_admin', 'sub_admin', 'ops_admin']}>
              <ActivityPage />
            </Protected>
          }
        />
        <Route
          path="/users"
          element={
            <Protected roles={['super_admin', 'sub_admin']}>
              <UsersPage />
            </Protected>
          }
        />
        <Route
          path="/database"
          element={
            <Protected roles={['super_admin']}>
              <DatabaseBrowserPage />
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
