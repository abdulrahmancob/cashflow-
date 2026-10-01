import { useEffect, useMemo, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { Download } from 'lucide-react'
import {
  analyticsApi,
  type AnalyticsGrain,
  type AnalyticsPreset,
  type AnalyticsTeamKey,
  type CollectionRootCauseBreakdown,
  type SsBreakdown,
  type SsBreakdownRow,
  type TeamPerson,
  type TeamSummary,
  type TeamUserDetail,
} from '../api/analytics'
import {
  Avatar,
  EmptyState,
  Table,
  TableCard,
  Td,
  Th,
  THead,
  Tr,
} from '../components/table'
import { Alert, Badge, Button, Card, Drawer, Input, Select } from '../components/ui'

const TEAM_SS = 'second_submission' as const
const TEAM_ELIGIBILITY = 'eligibility' as const
const TEAM_COLLECTION = 'collection' as const
const TEAM_SUBMISSION = 'submission' as const

const TEAM_COPY: Record<
  AnalyticsTeamKey,
  { title: string; subtitle: string; people: string; csv: string }
> = {
  second_submission: {
    title: 'Second Submission',
    subtitle: 'Claims counted when the person sets themselves as Submitter. Hours are active time on the system.',
    people:
      'Claims are Submitter = this person. Today / week / month use submission date, not Date of Service. Click a row for daily hours and recent claims.',
    csv: 'ss-team-analytics.csv',
  },
  eligibility: {
    title: 'Eligibility',
    subtitle: 'Sheet work for the posting team. Collection denied visits are counted on the Collection tab.',
    people:
      'Touched is distinct sheet items this person edited. Completed is terminal sheet status. Click a row for recent edits.',
    csv: 'eligibility-team-analytics.csv',
  },
  collection: {
    title: 'Collection',
    subtitle: 'Denied-visit collection queue. Counts are today and this month. Sheet eligibility is on the Eligibility tab.',
    people:
      'Edited is distinct claims this person changed. Assigned is claims you assigned them; Finished is how many of those they set a Collection Status on afterwards. Click a row for recent edits.',
    csv: 'collection-team-analytics.csv',
  },
  submission: {
    title: 'Submission',
    subtitle: 'CPT and ICD audit work. Demo findings are excluded. Resolved includes ignored.',
    people:
      'Touched is distinct audit items this person edited. Resolved is resolved or ignored. Click a row for recent edits.',
    csv: 'submission-team-analytics.csv',
  },
}

function formatHours(seconds: number) {
  const s = Math.max(0, Math.round(Number(seconds) || 0))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (h === 0 && m === 0) return s > 0 ? '<1m' : '0h'
  if (h === 0) return `${m}m`
  if (m === 0) return `${h}h`
  return `${h}h ${m}m`
}

function money(n: number) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 0,
  }).format(Number(n) || 0)
}

function formatWhen(iso?: string | null) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString()
}

function csvCell(value: string | number) {
  const text = String(value ?? '')
  if (/[",\n]/.test(text)) return `"${text.replace(/"/g, '""')}"`
  return text
}

function n(value: number | undefined) {
  return Number(value) || 0
}

function statusCount(person: TeamPerson, period: 'today' | 'month', label: string) {
  const bag = period === 'today' ? person.coll_status_today : person.coll_status_month
  return n(bag?.[label])
}

function submissionTouched(p: TeamPerson, suffix: '' | '_today' | '_week' | '_month') {
  return n(p[`cpt_touched${suffix}` as keyof TeamPerson] as number) + n(p[`icd_touched${suffix}` as keyof TeamPerson] as number)
}

function submissionResolved(p: TeamPerson, suffix: '' | '_today' | '_week' | '_month') {
  return n(p[`cpt_resolved${suffix}` as keyof TeamPerson] as number) + n(p[`icd_resolved${suffix}` as keyof TeamPerson] as number)
}

function exportCsv(people: TeamPerson[], team: AnalyticsTeamKey, statuses: string[] = []) {
  const hours = ['Hours today', 'Hours week', 'Hours month']
  const statusHeaders = [
    ...statuses.map((label) => `${label} today`),
    ...statuses.map((label) => `${label} month`),
  ]
  const headers =
    team === TEAM_ELIGIBILITY
      ? ['Name', 'Roles', ...hours, 'Touched today', 'Touched week', 'Touched month', 'Completed today', 'Completed week', 'Completed month', 'Sheet money']
      : team === TEAM_COLLECTION
        ? [
            'Name',
            'Roles',
            'Hours today',
            'Hours month',
            'Edited today',
            'Edited month',
            'Assigned',
            'Finished',
            ...statusHeaders,
            'Recovered today',
            'Recovered month',
            'Money today',
            'Money month',
          ]
        : team === TEAM_SUBMISSION
          ? ['Name', 'Roles', ...hours, 'Touched today', 'Touched week', 'Touched month', 'Resolved today', 'Resolved week', 'Resolved month', 'CPT resolved', 'ICD resolved']
          : [
              'Name',
              'Roles',
              ...hours,
              'Claims today',
              'Claims week',
              'Claims month',
              'Payment',
              'Paid',
              'Denied',
              'Timely Filing',
              'Pending',
              'Submitted',
              'Corrected',
            ]
  const lines = [
    headers.join(','),
    ...people.map((p) => {
      const common =
        team === TEAM_COLLECTION
          ? [
              p.display_name,
              (p.roles || []).join('|'),
              formatHours(p.seconds_today),
              formatHours(p.seconds_month),
            ]
          : [
              p.display_name,
              (p.roles || []).join('|'),
              formatHours(p.seconds_today),
              formatHours(p.seconds_week),
              formatHours(p.seconds_month),
            ]
      const rest =
        team === TEAM_ELIGIBILITY
          ? [
              p.elig_touched_today,
              p.elig_touched_week,
              p.elig_touched_month,
              p.elig_completed_today,
              p.elig_completed_week,
              p.elig_completed_month,
              p.elig_money,
            ]
          : team === TEAM_COLLECTION
            ? [
                p.coll_touched_today,
                p.coll_touched_month,
                p.coll_assigned,
                p.coll_finished,
                ...statuses.map((label) => statusCount(p, 'today', label)),
                ...statuses.map((label) => statusCount(p, 'month', label)),
                p.coll_recovered_today,
                p.coll_recovered_month,
                p.coll_money_today,
                p.coll_money_month,
              ]
            : team === TEAM_SUBMISSION
              ? [
                  submissionTouched(p, '_today'),
                  submissionTouched(p, '_week'),
                  submissionTouched(p, '_month'),
                  submissionResolved(p, '_today'),
                  submissionResolved(p, '_week'),
                  submissionResolved(p, '_month'),
                  p.cpt_resolved_month,
                  p.icd_resolved_month,
                ]
              : [
                  p.ss_claims_today,
                  p.ss_claims_week,
                  p.ss_claims_month,
                  p.ss_money,
                  p.ss_paid,
                  p.ss_denied,
                  p.ss_timely_filing,
                  p.ss_pending,
                  p.ss_submitted,
                  p.ss_corrected,
                ]
      return [...common, ...rest].map(csvCell).join(',')
    }),
  ]
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = TEAM_COPY[team].csv
  a.click()
  URL.revokeObjectURL(url)
}

const PRESETS: Array<{ id: AnalyticsPreset; label: string }> = [
  { id: 'today', label: 'Today' },
  { id: 'week', label: 'This week' },
  { id: 'month', label: 'This month' },
  { id: 'custom', label: 'Custom' },
]

const GRAINS: Array<{ id: AnalyticsGrain; label: string }> = [
  { id: 'day', label: 'Day' },
  { id: 'week', label: 'Week' },
  { id: 'month', label: 'Month' },
]

const GRID_COLS: Array<{
  key: keyof SsBreakdownRow
  label: string
  className: string
  format?: (n: number) => string
}> = [
  { key: 'payment', label: 'Payment', className: 'bg-amber-100 text-amber-900 dark:bg-amber-900/50 dark:text-amber-100', format: money },
  { key: 'claims', label: 'Claims', className: 'bg-slate-100 text-slate-800 dark:bg-slate-800 dark:text-slate-100' },
  { key: 'total_submitted_claims', label: 'Total submitted claims', className: 'bg-indigo-100 text-indigo-900 dark:bg-indigo-900/50 dark:text-indigo-100' },
  { key: 'submitted', label: 'Submitted', className: 'bg-sky-100 text-sky-900 dark:bg-sky-900/50 dark:text-sky-100' },
  { key: 'paid', label: 'Paid', className: 'bg-emerald-100 text-emerald-900 dark:bg-emerald-900/50 dark:text-emerald-100' },
  { key: 'pending', label: 'Pending', className: 'bg-emerald-50 text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200' },
  { key: 'denied', label: 'Denied', className: 'bg-rose-100 text-rose-900 dark:bg-rose-900/50 dark:text-rose-100' },
  { key: 'corrected', label: 'Corrected', className: 'bg-orange-100 text-orange-900 dark:bg-orange-900/50 dark:text-orange-100' },
  { key: 'timely_filing', label: 'Timely Filing', className: 'bg-pink-100 text-pink-900 dark:bg-pink-900/50 dark:text-pink-100' },
]

function currentYear() {
  return new Date().getFullYear()
}

function currentMonthValue() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

function StatusKpi({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: string | number
  hint?: string
  tone: 'default' | 'ok' | 'danger' | 'pink' | 'amber' | 'info'
}) {
  const tones: Record<string, string> = {
    default: 'border-gray-200 dark:border-gray-800',
    ok: 'border-emerald-200 bg-emerald-50/60 dark:border-emerald-900/60 dark:bg-emerald-950/30',
    danger: 'border-rose-200 bg-rose-50/60 dark:border-rose-900/60 dark:bg-rose-950/30',
    pink: 'border-pink-200 bg-pink-50/70 dark:border-pink-900/60 dark:bg-pink-950/30',
    amber: 'border-amber-200 bg-amber-50/70 dark:border-amber-900/60 dark:bg-amber-950/30',
    info: 'border-sky-200 bg-sky-50/50 dark:border-sky-900/60 dark:bg-sky-950/30',
  }
  const dots: Record<string, string> = {
    default: 'bg-gray-400',
    ok: 'bg-emerald-500',
    danger: 'bg-rose-500',
    pink: 'bg-pink-500',
    amber: 'bg-amber-500',
    info: 'bg-sky-500',
  }
  return (
    <div className={`rounded-xl border bg-white p-5 shadow-xs dark:bg-gray-900 ${tones[tone]}`}>
      <div className="flex items-center gap-2 text-sm font-medium text-gray-500 dark:text-gray-400">
        <span className={`h-1.5 w-1.5 rounded-full ${dots[tone]}`} />
        {label}
      </div>
      <div className="font-display mt-2 text-3xl font-semibold tracking-tight text-gray-900 dark:text-white">
        {value}
      </div>
      {hint && <div className="mt-1 text-xs text-gray-400">{hint}</div>}
    </div>
  )
}

function TeamKpis({ team, kpis }: { team: AnalyticsTeamKey; kpis: TeamSummary['kpis'] | undefined }) {
  if (team === TEAM_ELIGIBILITY) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4 2xl:grid-cols-7">
        <StatusKpi label="Touched today" value={kpis?.elig_touched_today || 0} tone="info" hint="Sheet edits" />
        <StatusKpi label="Touched week" value={kpis?.elig_touched_week || 0} tone="info" hint="Sheet edits" />
        <StatusKpi label="Touched month" value={kpis?.elig_touched_month || 0} tone="info" hint="Sheet edits" />
        <StatusKpi label="Completed today" value={kpis?.elig_completed_today || 0} tone="ok" hint="Completed / rejected" />
        <StatusKpi label="Completed week" value={kpis?.elig_completed_week || 0} tone="ok" hint="Completed / rejected" />
        <StatusKpi label="Completed month" value={kpis?.elig_completed_month || 0} tone="ok" hint="Completed / rejected" />
        <StatusKpi label="Sheet money" value={money(kpis?.elig_money || 0)} tone="amber" hint="Ledger in this period" />
      </div>
    )
  }
  if (team === TEAM_COLLECTION) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-6">
        <StatusKpi label="Edited today" value={kpis?.coll_touched_today || 0} tone="info" hint="Distinct claims" />
        <StatusKpi label="Edited month" value={kpis?.coll_touched_month || 0} tone="info" hint="Distinct claims" />
        <StatusKpi label="Recovered today" value={kpis?.coll_recovered_today || 0} tone="ok" hint="Paid after their work" />
        <StatusKpi label="Recovered month" value={kpis?.coll_recovered_month || 0} tone="ok" hint="Paid after their work" />
        <StatusKpi label="Money today" value={money(kpis?.coll_money_today || 0)} tone="amber" hint="Insurance + patient" />
        <StatusKpi label="Money month" value={money(kpis?.coll_money_month || 0)} tone="amber" hint="Insurance + patient" />
      </div>
    )
  }
  if (team === TEAM_SUBMISSION) {
    const touchedToday = n(kpis?.cpt_touched_today) + n(kpis?.icd_touched_today)
    const touchedWeek = n(kpis?.cpt_touched_week) + n(kpis?.icd_touched_week)
    const touchedMonth = n(kpis?.cpt_touched_month) + n(kpis?.icd_touched_month)
    const resolvedToday = n(kpis?.cpt_resolved_today) + n(kpis?.icd_resolved_today)
    const resolvedWeek = n(kpis?.cpt_resolved_week) + n(kpis?.icd_resolved_week)
    const resolvedMonth = n(kpis?.cpt_resolved_month) + n(kpis?.icd_resolved_month)
    return (
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4 2xl:grid-cols-8">
        <StatusKpi label="Touched today" value={touchedToday} tone="info" hint="CPT + ICD" />
        <StatusKpi label="Touched week" value={touchedWeek} tone="info" hint="CPT + ICD" />
        <StatusKpi label="Touched month" value={touchedMonth} tone="info" hint="CPT + ICD" />
        <StatusKpi label="Resolved today" value={resolvedToday} tone="ok" hint="Resolved / ignored" />
        <StatusKpi label="Resolved week" value={resolvedWeek} tone="ok" hint="Resolved / ignored" />
        <StatusKpi label="Resolved month" value={resolvedMonth} tone="ok" hint="Resolved / ignored" />
        <StatusKpi label="CPT resolved" value={kpis?.cpt_resolved_month || 0} tone="amber" hint="This month" />
        <StatusKpi label="ICD resolved" value={kpis?.icd_resolved_month || 0} tone="pink" hint="This month" />
      </div>
    )
  }
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4 2xl:grid-cols-7">
      <StatusKpi label="Claims today" value={kpis?.ss_claims_today || 0} tone="info" hint="Submitter / submission date" />
      <StatusKpi label="Claims week" value={kpis?.ss_claims_week || 0} tone="info" hint="Submitter / submission date" />
      <StatusKpi label="Claims month" value={kpis?.ss_claims_month || 0} tone="info" hint="Submitter / submission date" />
      <StatusKpi label="Payment" value={money(kpis?.ss_money || 0)} tone="amber" hint="Submitter / submission date" />
      <StatusKpi label="Paid" value={kpis?.ss_paid || 0} tone="ok" hint="Submitter / submission date" />
      <StatusKpi label="Denied" value={kpis?.ss_denied || 0} tone="danger" hint="Submitter / submission date" />
      <StatusKpi
        label="Timely Filing"
        value={kpis?.ss_timely_filing || 0}
        tone="pink"
        hint="Submitter / submission date"
      />
    </div>
  )
}

function PeopleHead({ team, statuses }: { team: AnalyticsTeamKey; statuses: string[] }) {
  return (
    <tr>
      <Th>Person</Th>
      <Th>Hours today</Th>
      {team !== TEAM_COLLECTION && <Th>Hours week</Th>}
      <Th>Hours month</Th>
      {team === TEAM_ELIGIBILITY && (
        <>
          <Th>Touched today</Th>
          <Th>Touched week</Th>
          <Th>Touched month</Th>
          <Th>Completed today</Th>
          <Th>Completed week</Th>
          <Th>Completed month</Th>
          <Th>Sheet money</Th>
        </>
      )}
      {team === TEAM_COLLECTION && (
        <>
          <Th>Edited today</Th>
          <Th>Edited month</Th>
          <Th>Assigned</Th>
          <Th>Finished</Th>
          {statuses.map((label) => (
            <Th key={`${label}-today`}>{label} today</Th>
          ))}
          {statuses.map((label) => (
            <Th key={`${label}-month`}>{label} month</Th>
          ))}
          <Th>Recovered today</Th>
          <Th>Recovered month</Th>
          <Th>Money today</Th>
          <Th>Money month</Th>
        </>
      )}
      {team === TEAM_SUBMISSION && (
        <>
          <Th>Touched today</Th>
          <Th>Touched week</Th>
          <Th>Touched month</Th>
          <Th>Resolved today</Th>
          <Th>Resolved week</Th>
          <Th>Resolved month</Th>
          <Th>CPT resolved</Th>
          <Th>ICD resolved</Th>
        </>
      )}
      {team === TEAM_SS && (
        <>
          <Th>Claims today</Th>
          <Th>Claims week</Th>
          <Th>Claims month</Th>
          <Th>Paid</Th>
          <Th>Denied</Th>
          <Th>Timely Filing</Th>
        </>
      )}
    </tr>
  )
}

function PeopleCells({
  team,
  person,
  statuses,
}: {
  team: AnalyticsTeamKey
  person: TeamPerson
  statuses: string[]
}) {
  return (
    <>
      <Td className="tabular-nums">{formatHours(person.seconds_today)}</Td>
      {team !== TEAM_COLLECTION && <Td className="tabular-nums">{formatHours(person.seconds_week)}</Td>}
      <Td className="tabular-nums">{formatHours(person.seconds_month)}</Td>
      {team === TEAM_ELIGIBILITY && (
        <>
          <Td className="tabular-nums">{person.elig_touched_today}</Td>
          <Td className="tabular-nums">{person.elig_touched_week}</Td>
          <Td className="tabular-nums">{person.elig_touched_month}</Td>
          <Td className="tabular-nums">{person.elig_completed_today}</Td>
          <Td className="tabular-nums">{person.elig_completed_week}</Td>
          <Td className="tabular-nums">{person.elig_completed_month}</Td>
          <Td className="tabular-nums">{money(person.elig_money)}</Td>
        </>
      )}
      {team === TEAM_COLLECTION && (
        <>
          <Td className="tabular-nums">{person.coll_touched_today}</Td>
          <Td className="tabular-nums">{person.coll_touched_month}</Td>
          <Td className="tabular-nums">{person.coll_assigned}</Td>
          <Td className="tabular-nums">{person.coll_finished}</Td>
          {statuses.map((label) => (
            <Td key={`${label}-today`} className="tabular-nums">
              {statusCount(person, 'today', label)}
            </Td>
          ))}
          {statuses.map((label) => (
            <Td key={`${label}-month`} className="tabular-nums">
              {statusCount(person, 'month', label)}
            </Td>
          ))}
          <Td className="tabular-nums">{person.coll_recovered_today}</Td>
          <Td className="tabular-nums">{person.coll_recovered_month}</Td>
          <Td className="tabular-nums">{money(person.coll_money_today)}</Td>
          <Td className="tabular-nums">{money(person.coll_money_month)}</Td>
        </>
      )}
      {team === TEAM_SUBMISSION && (
        <>
          <Td className="tabular-nums">{submissionTouched(person, '_today')}</Td>
          <Td className="tabular-nums">{submissionTouched(person, '_week')}</Td>
          <Td className="tabular-nums">{submissionTouched(person, '_month')}</Td>
          <Td className="tabular-nums">{submissionResolved(person, '_today')}</Td>
          <Td className="tabular-nums">{submissionResolved(person, '_week')}</Td>
          <Td className="tabular-nums">{submissionResolved(person, '_month')}</Td>
          <Td>
            <Badge tone="green">{person.cpt_resolved_month}</Badge>
          </Td>
          <Td>
            <Badge tone="purple">{person.icd_resolved_month}</Badge>
          </Td>
        </>
      )}
      {team === TEAM_SS && (
        <>
          <Td className="tabular-nums">{person.ss_claims_today}</Td>
          <Td className="tabular-nums">{person.ss_claims_week}</Td>
          <Td className="tabular-nums">{person.ss_claims_month}</Td>
          <Td>
            <Badge tone="green">{person.ss_paid}</Badge>
          </Td>
          <Td>
            <Badge tone="red">{person.ss_denied}</Badge>
          </Td>
          <Td>
            <span className="inline-flex rounded-full bg-pink-50 px-2 py-0.5 text-xs font-semibold text-pink-700 dark:bg-pink-950 dark:text-pink-300">
              {person.ss_timely_filing}
            </span>
          </Td>
        </>
      )}
    </>
  )
}

export function TeamAnalyticsPage() {
  const [team, setTeam] = useState<AnalyticsTeamKey>(TEAM_SS)
  const [preset, setPreset] = useState<AnalyticsPreset>('month')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [q, setQ] = useState('')
  const [data, setData] = useState<TeamSummary | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<TeamUserDetail | null>(null)
  const [detailError, setDetailError] = useState('')

  const [grain, setGrain] = useState<AnalyticsGrain>('month')
  const [year, setYear] = useState(currentYear())
  const [month, setMonth] = useState(currentMonthValue())
  const [memberId, setMemberId] = useState('')
  const [grid, setGrid] = useState<SsBreakdown | null>(null)
  const [gridError, setGridError] = useState('')
  const [gridLoading, setGridLoading] = useState(true)
  const [causes, setCauses] = useState<CollectionRootCauseBreakdown | null>(null)
  const [causeError, setCauseError] = useState('')
  const [causeLoading, setCauseLoading] = useState(false)

  const years = useMemo(() => {
    const y = currentYear()
    return [y - 1, y, y + 1]
  }, [])

  const isSsTeam = team === TEAM_SS
  const copy = TEAM_COPY[team] || TEAM_COPY.second_submission

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    void analyticsApi
      .team(preset, dateFrom, dateTo, team)
      .then((row) => {
        if (!cancelled) setData(row)
      })
      .catch((e) => {
        if (!cancelled) setError(String((e as Error).message))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [preset, dateFrom, dateTo, team])

  useEffect(() => {
    if (!selectedId) {
      setDetail(null)
      return
    }
    let cancelled = false
    setDetailError('')
    void analyticsApi
      .user(selectedId, preset, dateFrom, dateTo, team)
      .then((row) => {
        if (!cancelled) setDetail(row)
      })
      .catch((e) => {
        if (!cancelled) setDetailError(String((e as Error).message))
      })
    return () => {
      cancelled = true
    }
  }, [selectedId, preset, dateFrom, dateTo, team])

  useEffect(() => {
    if (!isSsTeam) {
      setGrid(null)
      setGridLoading(false)
      setGridError('')
      return
    }
    let cancelled = false
    setGridLoading(true)
    setGridError('')
    void analyticsApi
      .ssBreakdown({
        grain,
        year,
        month: grain === 'day' ? month : undefined,
        userId: memberId || undefined,
      })
      .then((row) => {
        if (!cancelled) setGrid(row)
      })
      .catch((e) => {
        if (!cancelled) setGridError(String((e as Error).message))
      })
      .finally(() => {
        if (!cancelled) setGridLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [isSsTeam, grain, year, month, memberId])

  const isCollectionTeam = team === TEAM_COLLECTION

  useEffect(() => {
    if (!isCollectionTeam) {
      setCauses(null)
      setCauseLoading(false)
      setCauseError('')
      return
    }
    let cancelled = false
    setCauseLoading(true)
    setCauseError('')
    void analyticsApi
      .collectionRootCauses(year)
      .then((row) => {
        if (!cancelled) setCauses(row)
      })
      .catch((e) => {
        if (!cancelled) setCauseError(String((e as Error).message))
      })
      .finally(() => {
        if (!cancelled) setCauseLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [isCollectionTeam, year])

  const people = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const rows = data?.people || []
    if (!needle) return rows
    return rows.filter(
      (p) =>
        p.display_name.toLowerCase().includes(needle) ||
        p.username.toLowerCase().includes(needle) ||
        p.roles.some((r) => r.toLowerCase().includes(needle)),
    )
  }, [data, q])

  const statuses = data?.collection_statuses || []
  const kpis = data?.kpis
  const teams = data?.teams?.length
    ? data.teams
    : [{ key: TEAM_SS, label: 'Second Submission' }]
  const memberOptions = grid?.people?.length
    ? grid.people
    : (data?.people || []).map((p) => ({ user_id: p.user_id, display_name: p.display_name }))

  return (
    <div className="space-y-6">
      <PageChrome
        title={copy.title}
        subtitle={copy.subtitle}
        onExport={() => people.length && exportCsv(people, team, statuses)}
        canExport={people.length > 0}
      />

      <div className="flex flex-wrap gap-1 rounded-lg bg-gray-100 p-1 dark:bg-gray-800">
        {teams.map((t) => (
          <button
            key={t.key}
            type="button"
            className={`rounded-md px-3 py-1.5 text-sm font-medium ${
              team === t.key
                ? 'bg-white text-gray-900 shadow-xs dark:bg-gray-900 dark:text-white'
                : 'text-gray-600 hover:text-gray-900 dark:text-gray-300'
            }`}
            onClick={() => {
              setTeam(t.key)
              setSelectedId(null)
              setMemberId('')
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-wrap gap-1 rounded-lg bg-gray-100 p-1 dark:bg-gray-800">
          {PRESETS.map((p) => (
            <button
              key={p.id}
              type="button"
              className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                preset === p.id
                  ? 'bg-white text-gray-900 shadow-xs dark:bg-gray-900 dark:text-white'
                  : 'text-gray-600 hover:text-gray-900 dark:text-gray-300'
              }`}
              onClick={() => setPreset(p.id)}
            >
              {p.label}
            </button>
          ))}
        </div>
        {preset === 'custom' && (
          <>
            <label className="text-sm text-gray-600 dark:text-gray-300">
              From
              <Input
                type="date"
                className="mt-1"
                value={dateFrom}
                onChange={(e) => setDateFrom(e.target.value)}
              />
            </label>
            <label className="text-sm text-gray-600 dark:text-gray-300">
              To
              <Input
                type="date"
                className="mt-1"
                value={dateTo}
                onChange={(e) => setDateTo(e.target.value)}
              />
            </label>
          </>
        )}
        <Input
          className="w-56"
          placeholder="Search people"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>

      {error && <Alert>{error}</Alert>}

      <TeamKpis team={team} kpis={kpis} />

      <div className={`grid gap-4 ${isSsTeam ? 'xl:grid-cols-2' : ''}`}>
        <Card title="Hours by person" description="Active time in the selected period">
          {data?.charts.hours.length ? (
            <div className="h-72">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={data.charts.hours} margin={{ top: 8, right: 8, left: 0, bottom: 24 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
                  <XAxis dataKey="name" tick={{ fontSize: 11 }} interval={0} angle={-25} textAnchor="end" height={60} />
                  <YAxis tick={{ fontSize: 11 }} />
                  <Tooltip />
                  <Bar dataKey="hours" name="Hours" fill="#2563eb" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <EmptyState title="No tracked hours yet" description="Hours start after this feature is deployed." />
          )}
        </Card>
        {isSsTeam && (
          <Card title="Second Submission outcomes" description="Paid / denied / Timely Filing">
            {data?.charts.outcomes.length ? (
              <div className="h-72">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={data.charts.outcomes} margin={{ top: 8, right: 8, left: 0, bottom: 24 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
                    <XAxis dataKey="name" tick={{ fontSize: 11 }} interval={0} angle={-25} textAnchor="end" height={60} />
                    <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                    <Tooltip />
                    <Legend />
                    <Bar dataKey="paid" name="Paid" stackId="a" fill="#12b76a" />
                    <Bar dataKey="denied" name="Denied" stackId="a" fill="#ef4444" />
                    <Bar dataKey="timely_filing" name="Timely Filing" stackId="a" fill="#ec4899" />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <EmptyState title="No claims in this period" />
            )}
          </Card>
        )}
      </div>

      <TableCard
        title="People"
        count={people.length}
        countLabel="people"
        description={loading ? 'Loading…' : copy.people}
      >
        {people.length ? (
          <div className="overflow-x-auto">
            <Table>
              <THead>
                <PeopleHead team={team} statuses={statuses} />
              </THead>
              <tbody>
                {people.map((p) => (
                  <Tr
                    key={p.user_id}
                    className="cursor-pointer"
                    onClick={() => setSelectedId(p.user_id)}
                  >
                    <Td>
                      <div className="flex items-center gap-2">
                        <Avatar name={p.display_name} size="sm" />
                        <div>
                          <div className="font-medium text-gray-900 dark:text-white">{p.display_name}</div>
                          <div className="text-xs text-gray-500">{(p.roles || []).join(', ')}</div>
                        </div>
                      </div>
                    </Td>
                    <PeopleCells team={team} person={p} statuses={statuses} />
                  </Tr>
                ))}
              </tbody>
            </Table>
          </div>
        ) : (
          <EmptyState
            title={loading ? 'Loading team' : 'No people in scope'}
            description={loading ? undefined : 'Leads only see their own team.'}
          />
        )}
      </TableCard>

      {isCollectionTeam && (
        <TableCard
          title="Root cause by month"
          count={causes?.rows.length}
          countLabel="months"
          description={
            causeLoading
              ? 'Loading…'
              : 'Months follow Date of Service. Each claim counts once, under the last root cause saved on it. The people table is who wrote that last value.'
          }
        >
          <div className="flex flex-wrap items-end gap-3 border-b border-gray-100 px-5 py-3 dark:border-gray-800">
            <label className="text-sm text-gray-600 dark:text-gray-300">
              Year
              <Select
                className="mt-1 w-28"
                value={String(year)}
                onChange={(e) => setYear(Number(e.target.value))}
              >
                {years.map((y) => (
                  <option key={y} value={y}>
                    {y}
                  </option>
                ))}
              </Select>
            </label>
          </div>
          {causeError && (
            <div className="px-5 pt-3">
              <Alert>{causeError}</Alert>
            </div>
          )}
          {causes?.rows.length ? (
            <div className="overflow-x-auto">
              <Table>
                <THead>
                  <tr>
                    <Th>Month</Th>
                    <Th>Top</Th>
                    {(causes.labels || []).map((label) => (
                      <Th key={label}>{label}</Th>
                    ))}
                  </tr>
                </THead>
                <tbody>
                  {causes.rows.map((row) => (
                    <Tr key={row.period_start || row.period}>
                      <Td>{row.period}</Td>
                      <Td>
                        {row.top ? (
                          <Badge tone="purple">
                            {row.top} ({row.top_count})
                          </Badge>
                        ) : (
                          '—'
                        )}
                      </Td>
                      {(causes.labels || []).map((label) => {
                        const value = n(row.counts?.[label])
                        const isTop = Boolean(row.top) && label === row.top && value > 0
                        return (
                          <Td
                            key={label}
                            className={`tabular-nums ${isTop ? 'font-semibold text-gray-900 dark:text-white' : ''}`}
                          >
                            {value}
                          </Td>
                        )
                      })}
                    </Tr>
                  ))}
                </tbody>
              </Table>
            </div>
          ) : (
            <EmptyState title={causeLoading ? 'Loading root causes' : 'No root causes in this year'} />
          )}
          {causes && (
          <div className="border-t border-gray-100 dark:border-gray-800">
            <div className="px-5 py-3 text-sm text-gray-500 dark:text-gray-400">
              Each person, for claims whose Date of Service is in {year}.
            </div>
            {(causes?.people || []).length ? (
              <div className="overflow-x-auto">
                <Table>
                  <THead>
                    <tr>
                      <Th>Person</Th>
                      <Th>Top</Th>
                      {(causes?.labels || []).map((label) => (
                        <Th key={label}>{label}</Th>
                      ))}
                    </tr>
                  </THead>
                  <tbody>
                    {(causes?.people || []).map((person) => (
                      <Tr key={person.user_id}>
                        <Td>{person.display_name}</Td>
                        <Td>
                          {person.top ? (
                            <Badge tone="purple">
                              {person.top} ({person.top_count})
                            </Badge>
                          ) : (
                            '—'
                          )}
                        </Td>
                        {(causes?.labels || []).map((label) => {
                          const value = n(person.counts?.[label])
                          const isTop = Boolean(person.top) && label === person.top && value > 0
                          return (
                            <Td
                              key={label}
                              className={`tabular-nums ${isTop ? 'font-semibold text-gray-900 dark:text-white' : ''}`}
                            >
                              {value}
                            </Td>
                          )
                        })}
                      </Tr>
                    ))}
                  </tbody>
                </Table>
              </div>
            ) : (
              <EmptyState title="No collectors in scope" />
            )}
          </div>
          )}
        </TableCard>
      )}

      {isSsTeam && (
      <TableCard
        title="Claim analysis"
        count={grid?.rows.length}
        countLabel={grain === 'day' ? 'days' : grain === 'week' ? 'weeks' : 'months'}
        description={
          gridLoading
            ? 'Loading…'
            : memberId
              ? 'Claims this person set as Submitter.'
              : 'All team is Date of Service for every clinic visit (Payment / Status). Pick a member to see only their Submitter rows.'
        }
      >
        <div className="flex flex-wrap items-end gap-3 border-b border-gray-100 px-5 py-3 dark:border-gray-800">
          <div className="flex flex-wrap gap-1 rounded-lg bg-gray-100 p-1 dark:bg-gray-800">
            {GRAINS.map((g) => (
              <button
                key={g.id}
                type="button"
                className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                  grain === g.id
                    ? 'bg-white text-gray-900 shadow-xs dark:bg-gray-900 dark:text-white'
                    : 'text-gray-600 hover:text-gray-900 dark:text-gray-300'
                }`}
                onClick={() => setGrain(g.id)}
              >
                {g.label}
              </button>
            ))}
          </div>
          <label className="text-sm text-gray-600 dark:text-gray-300">
            Year
            <Select
              className="mt-1 w-28"
              value={String(year)}
              onChange={(e) => setYear(Number(e.target.value))}
            >
              {years.map((y) => (
                <option key={y} value={y}>
                  {y}
                </option>
              ))}
            </Select>
          </label>
          {grain === 'day' && (
            <label className="text-sm text-gray-600 dark:text-gray-300">
              Month
              <Input
                type="month"
                className="mt-1 w-40"
                value={month}
                onChange={(e) => {
                  setMonth(e.target.value)
                  const y = Number(e.target.value.slice(0, 4))
                  if (y) setYear(y)
                }}
              />
            </label>
          )}
          <label className="text-sm text-gray-600 dark:text-gray-300">
            Member
            <Select
              className="mt-1 w-56"
              value={memberId}
              onChange={(e) => setMemberId(e.target.value)}
            >
              <option value="">All team</option>
              {memberOptions.map((p) => (
                <option key={p.user_id} value={p.user_id}>
                  {p.display_name}
                </option>
              ))}
            </Select>
          </label>
        </div>
        {gridError && <div className="px-5 pt-3"><Alert>{gridError}</Alert></div>}
        {grid?.rows.length ? (
          <div className="overflow-x-auto">
            <Table>
              <THead>
                <tr>
                  <Th>Period</Th>
                  {GRID_COLS.map((col) => (
                    <Th key={col.key} className={col.className}>
                      {col.label}
                    </Th>
                  ))}
                </tr>
              </THead>
              <tbody>
                {grid.rows.map((row) => (
                  <GridRow key={row.period_start || row.period} row={row} />
                ))}
                <GridRow row={grid.totals} total />
              </tbody>
            </Table>
          </div>
        ) : (
          <EmptyState title={gridLoading ? 'Loading analysis' : 'No claims in this window'} />
        )}
      </TableCard>
      )}

      <Drawer
        open={Boolean(selectedId)}
        onClose={() => setSelectedId(null)}
        title={detail?.user.display_name || 'Person'}
        wide
      >
        {detailError && <Alert>{detailError}</Alert>}
        {!detail && !detailError && <p className="text-sm text-gray-500">Loading…</p>}
        {detail && (
          <div className="space-y-5">
            <p className="text-sm text-gray-500">
              {detail.user.roles.join(', ')} · last login {formatWhen(detail.user.last_login_at)}
            </p>
            <Card title="Hours by day" padded={false}>
              {detail.hours.length ? (
                <Table>
                  <THead>
                    <tr>
                      <Th>Day</Th>
                      <Th>Time</Th>
                    </tr>
                  </THead>
                  <tbody>
                    {detail.hours.map((h) => (
                      <Tr key={h.day}>
                        <Td>{h.day}</Td>
                        <Td className="tabular-nums">{formatHours(h.seconds)}</Td>
                      </Tr>
                    ))}
                  </tbody>
                </Table>
              ) : (
                <div className="p-4 text-sm text-gray-500">No active time in this period.</div>
              )}
            </Card>
            {team === TEAM_SS && (
              <Card title="Recent Second Submission claims" padded={false}>
                <SampleTable
                  rows={detail.second_submission}
                  cols={[
                    ['dos', 'DOS'],
                    ['workload_status', 'Status'],
                    ['payment', 'Payment'],
                    ['claim_number', 'Claim'],
                    ['submitter', 'Submitter'],
                    ['submission_date', 'Submitted'],
                  ]}
                />
              </Card>
            )}
            {team === TEAM_ELIGIBILITY && (
              <Card title="Recent sheet edits" padded={false}>
                <SampleTable
                  rows={detail.eligibility || []}
                  cols={[
                    ['dos', 'DOS'],
                    ['patient_name', 'Patient'],
                    ['facility_name', 'Clinic'],
                    ['eligibility_status', 'Status'],
                    ['column_name', 'Field'],
                    ['new_value', 'Value'],
                    ['changed_at', 'When'],
                  ]}
                />
              </Card>
            )}
            {team === TEAM_COLLECTION && (
              <Card title="Recent collection edits" padded={false}>
                <SampleTable
                  rows={detail.collection || []}
                  cols={[
                    ['dos', 'DOS'],
                    ['patient_name', 'Patient'],
                    ['facility_name', 'Clinic'],
                    ['collection_status', 'Status'],
                    ['column_name', 'Field'],
                    ['new_value', 'Value'],
                    ['changed_at', 'When'],
                  ]}
                />
              </Card>
            )}
            {team === TEAM_SUBMISSION && (
              <Card title="Recent CPT / ICD edits" padded={false}>
                <SampleTable
                  rows={detail.cpt_audit || []}
                  cols={[
                    ['dos', 'DOS'],
                    ['patient_name', 'Patient'],
                    ['audit_domain', 'Type'],
                    ['workflow_status', 'Status'],
                    ['rule_code', 'Rule'],
                    ['new_value', 'Value'],
                    ['changed_at', 'When'],
                  ]}
                />
              </Card>
            )}
          </div>
        )}
      </Drawer>
    </div>
  )
}

function PageChrome({
  title,
  subtitle,
  onExport,
  canExport,
}: {
  title: string
  subtitle: string
  onExport: () => void
  canExport: boolean
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="font-display text-2xl font-semibold tracking-tight text-gray-900 dark:text-white">
          {title}
        </h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          {subtitle}
        </p>
      </div>
      <Button
        type="button"
        variant="secondary"
        className="inline-flex items-center"
        onClick={onExport}
        disabled={!canExport}
      >
        <Download className="mr-1.5 h-4 w-4" />
        Export CSV
      </Button>
    </div>
  )
}

function GridRow({ row, total = false }: { row: SsBreakdownRow; total?: boolean }) {
  return (
    <Tr className={total ? 'bg-gray-50 font-semibold dark:bg-gray-800/60' : undefined}>
      <Td>{row.period}</Td>
      {GRID_COLS.map((col) => {
        const value = Number(row[col.key] || 0)
        return (
          <Td key={col.key} className="tabular-nums">
            {col.format ? col.format(value) : value}
          </Td>
        )
      })}
    </Tr>
  )
}

function SampleTable({
  rows,
  cols,
}: {
  rows: Array<Record<string, string | null>>
  cols: Array<[string, string]>
}) {
  if (!rows.length) {
    return <div className="p-4 text-sm text-gray-500">None in this period.</div>
  }
  return (
    <Table>
      <THead>
        <tr>
          {cols.map(([key, label]) => (
            <Th key={key}>{label}</Th>
          ))}
        </tr>
      </THead>
      <tbody>
        {rows.map((row, i) => (
          <Tr key={i}>
            {cols.map(([key]) => (
              <Td key={key} className="max-w-[12rem] truncate text-xs">
                {key.endsWith('_at') ? formatWhen(row[key]) : row[key] || '—'}
              </Td>
            ))}
          </Tr>
        ))}
      </tbody>
    </Table>
  )
}
