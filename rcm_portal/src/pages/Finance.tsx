import { useEffect, useState } from 'react'
import { Navigate, useNavigate, useParams } from 'react-router-dom'
import { type Filters } from '../api/forecast'
import { FinanceFilters } from '../components/FinanceFilters'
import { Alert, PageHeader } from '../components/ui'
import { BusinessInsights } from './finance/BusinessInsights'
import { CashTrajectory } from './finance/CashTrajectory'
import { MissionControl } from './finance/MissionControl'
import { OverdueClaims } from './finance/OverdueClaims'

const empty: Filters = {
  facility: [],
  ins: [],
  stage: [],
  month: [],
  dateFrom: '',
  dateTo: '',
  severity: [],
  riskFlag: [],
  q: '',
}

const titles: Record<string, string> = {
  mission: 'Mission Control',
  cash: 'Cash Trajectory',
  insights: 'Business Insights',
  overdue: 'Overdue',
  drill: 'Overdue',
}

const descriptions: Record<string, string> = {
  mission: 'RCM view: where the money is stuck, and what to chase today.',
  cash: 'CFO view: cash timing and forecast trust.',
  insights: 'CEO view: is the business healthier than last month, and where revenue leaks.',
  overdue: 'Claim-level overdue worklist for the RCM team.',
}

export function FinancePage() {
  const { tab } = useParams()
  const navigate = useNavigate()
  const view = tab || 'cash'
  const [filters, setFilters] = useState<Filters>(empty)
  const [error, setError] = useState('')

  useEffect(() => {
    setError('')
  }, [view])

  if (tab === 'exec') return <Navigate to="/finance/cash" replace />
  if (tab === 'drill') return <Navigate to="/finance/overdue" replace />

  return (
    <div className="space-y-6">
      <PageHeader title={titles[view] || 'Finance'} description={descriptions[view] || 'Read-only finance dashboards in Remitarc.'} />
      {error && <Alert>{error}</Alert>}
      <FinanceFilters filters={filters} onChange={setFilters} />
      {view === 'mission' && (
        <MissionControl
          filters={filters}
          onError={setError}
          onOpenInsurance={(ins) => {
            setFilters((current) => ({ ...current, ins: ins ? [ins] : current.ins, q: '' }))
            navigate('/finance/overdue')
          }}
          onOpenClaim={(row) => {
            setFilters((current) => ({
              ...current,
              ins: row.ins_name ? [row.ins_name] : current.ins,
              q: row.emr_patient_id || current.q,
            }))
            navigate('/finance/overdue')
          }}
        />
      )}
      {view === 'cash' && <CashTrajectory filters={filters} onError={setError} />}
      {view === 'insights' && <BusinessInsights filters={filters} onError={setError} />}
      {view === 'overdue' && (
        <OverdueClaims filters={filters} onChangeFilters={setFilters} onError={setError} />
      )}
    </div>
  )
}
