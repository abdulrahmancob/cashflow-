import { useEffect, useMemo, useState } from 'react'
import { ChevronLeft } from 'lucide-react'
import { ApiError } from '../api/client'
import { forecastApi, money } from '../api/forecast'
import {
  EmptyState,
  ErrorState,
  FilterBar,
  Pagination,
  Table,
  TableCard,
  Td,
  Th,
  THead,
  Tr,
  ViewTabs,
} from '../components/table'
import { Button, KpiCard, PageHeader } from '../components/ui'

type WaystarRow = {
  check_number: string
  payer: string
  claim_count: number
  amount: number
  latest_date: string | null
}

type TrackerRow = {
  row_id: string
  txn_date: string | null
  check_number: string
  description: string
  transaction_type: string
  amount: number
}

type PayerSummary = {
  payer: string
  checkCount: number
  amount: number
  latestDate: string | null
}

type Side = 'waystar' | 'tracker'

const PAGE_SIZE = 50

const SIDES: Array<{ key: Side; label: string }> = [
  { key: 'waystar', label: 'Waystar, not in tracker' },
  { key: 'tracker', label: 'Tracker, not in Waystar' },
]

function fmtDate(value: string | null | undefined) {
  if (!value) return '—'
  const s = String(value).slice(0, 10)
  const [y, m, day] = s.split('-')
  if (!y || !m || !day) return s
  return `${Number(m)}/${Number(day)}/${y}`
}

function includes(haystack: string, query: string) {
  return haystack.toLowerCase().includes(query)
}

function laterDate(current: string | null, next: string | null) {
  if (!next) return current
  if (!current || next > current) return next
  return current
}

function errorText(err: unknown) {
  if (err instanceof ApiError) {
    if (typeof err.detail === 'string' && err.detail.trim()) return err.detail
    return err.message || 'Could not load checks.'
  }
  if (err instanceof Error && err.message) return err.message
  return 'Could not load checks.'
}

export function UnbankedCashPage() {
  const [side, setSide] = useState<Side>('waystar')
  const [payer, setPayer] = useState('')
  const [selectedPayer, setSelectedPayer] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [waystar, setWaystar] = useState<WaystarRow[]>([])
  const [tracker, setTracker] = useState<TrackerRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    forecastApi
      .unbanked()
      .then((payload) => {
        if (cancelled) return
        setWaystar(payload.waystar_missing || [])
        setTracker(payload.tracker_missing || [])
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setWaystar([])
        setTracker([])
        setError(errorText(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [reloadKey])

  const query = payer.trim().toLowerCase()

  const waystarVisible = useMemo(
    () =>
      waystar.filter((row) => {
        if (!query) return true
        return includes(`${row.payer} ${row.check_number}`, query)
      }),
    [waystar, query],
  )
  const trackerVisible = useMemo(
    () =>
      tracker.filter((row) => {
        if (!query) return true
        return includes(`${row.check_number} ${row.description} ${row.transaction_type}`, query)
      }),
    [tracker, query],
  )
  const summaries = useMemo(() => {
    const byPayer = new Map<string, PayerSummary>()
    for (const row of waystarVisible) {
      const name = row.payer || 'Unknown'
      const current = byPayer.get(name) || {
        payer: name,
        checkCount: 0,
        amount: 0,
        latestDate: null,
      }
      current.checkCount += 1
      current.amount += Number(row.amount || 0)
      current.latestDate = laterDate(current.latestDate, row.latest_date)
      byPayer.set(name, current)
    }
    return [...byPayer.values()].sort((a, b) => b.amount - a.amount || a.payer.localeCompare(b.payer))
  }, [waystarVisible])

  const payerChecks = useMemo(() => {
    if (!selectedPayer) return []
    return waystarVisible
      .filter((row) => (row.payer || 'Unknown') === selectedPayer)
      .sort((a, b) => (b.latest_date || '').localeCompare(a.latest_date || '') || b.amount - a.amount)
  }, [waystarVisible, selectedPayer])

  const activeRows = side === 'waystar' ? (selectedPayer ? payerChecks : summaries) : trackerVisible
  const pages = Math.max(1, Math.ceil(activeRows.length / PAGE_SIZE))
  const safePage = Math.min(page, pages)
  const slice = activeRows.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE)

  const visibleChecks = side === 'waystar' ? waystarVisible : trackerVisible
  const scopedChecks = side === 'waystar' && selectedPayer ? payerChecks : visibleChecks
  const total = scopedChecks.reduce((sum, row) => sum + Number(row.amount || 0), 0)

  const tabs = SIDES.map((item) => ({
    key: item.key,
    label: item.label,
    count: item.key === 'waystar' ? waystarVisible.length : trackerVisible.length,
  }))

  function openPayer(name: string) {
    setSelectedPayer(name)
    setPage(1)
  }

  function showAllInsurances() {
    setSelectedPayer(null)
    setPage(1)
  }

  const drilled = side === 'waystar' && selectedPayer != null
  const title = drilled ? selectedPayer : side === 'waystar' ? 'Missing by insurance' : 'Tracker checks'
  const description = drilled
    ? 'Checks on a Waystar remit that are not on the transaction tracker.'
    : side === 'waystar'
      ? 'Each insurance, then the missing checks with date and amount.'
      : 'Tracker payments with a check or EFT number that is not on a Waystar remit.'

  return (
    <div className="space-y-6">
      <PageHeader
        title="Checks vs tracker"
        description="Waystar remits that never landed on the tracker, and tracker checks that are not on a Waystar remit."
      />

      <div className="grid gap-4 sm:grid-cols-2">
        <KpiCard label="Checks in view" value={loading ? '—' : scopedChecks.length.toLocaleString()} />
        <KpiCard label="Amount in view" value={loading ? '—' : money(total)} tone="info" />
      </div>

      <TableCard title={title} count={loading ? undefined : activeRows.length} countLabel={drilled ? 'checks' : side === 'waystar' ? 'insurances' : 'checks'} description={description}>
        <FilterBar
          tabs={
            <ViewTabs
              items={tabs}
              value={side}
              onChange={(key) => {
                setSide(key as Side)
                setSelectedPayer(null)
                setPage(1)
              }}
            />
          }
          search={payer}
          onSearch={(value) => {
            setPayer(value)
            setPage(1)
          }}
          searchPlaceholder="Search payer or check"
          extra={
            drilled ? (
              <Button type="button" variant="secondary" size="sm" onClick={showAllInsurances}>
                <ChevronLeft className="h-4 w-4" />
                All insurances
              </Button>
            ) : undefined
          }
        />
        {loading ? (
          <div className="flex min-h-48 items-center justify-center">
            <div className="h-8 w-8 animate-spin rounded-full border-2 border-gray-200 border-t-brand-600" />
          </div>
        ) : error ? (
          <ErrorState
            title="Could not load checks"
            description={error}
            onRetry={() => setReloadKey((n) => n + 1)}
          />
        ) : side === 'waystar' && !drilled ? (
          summaries.length ? (
            <Table>
              <THead>
                <tr>
                  <Th>Insurance</Th>
                  <Th>Checks</Th>
                  <Th>Amount</Th>
                  <Th>Latest date</Th>
                </tr>
              </THead>
              <tbody>
                {(slice as PayerSummary[]).map((row) => (
                  <Tr key={row.payer} onClick={() => openPayer(row.payer)}>
                    <Td>{row.payer}</Td>
                    <Td>{row.checkCount.toLocaleString()}</Td>
                    <Td>{money(row.amount)}</Td>
                    <Td>{fmtDate(row.latestDate)}</Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <EmptyState
              title={waystar.length ? 'No insurances in this view' : 'No missing Waystar checks'}
              description={
                waystar.length
                  ? 'Try another payer or check number.'
                  : 'Every Waystar check number is already on the tracker.'
              }
            />
          )
        ) : side === 'waystar' ? (
          payerChecks.length ? (
            <Table>
              <THead>
                <tr>
                  <Th>Check</Th>
                  <Th>Latest date</Th>
                  <Th>Claims</Th>
                  <Th>Amount</Th>
                </tr>
              </THead>
              <tbody>
                {(slice as WaystarRow[]).map((row) => (
                  <Tr key={`${row.check_number}-${row.latest_date}-${row.amount}`}>
                    <Td>{row.check_number || '—'}</Td>
                    <Td>{fmtDate(row.latest_date)}</Td>
                    <Td>{row.claim_count.toLocaleString()}</Td>
                    <Td>{money(row.amount)}</Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <EmptyState
              title="No checks for this insurance"
              description="The search does not match any missing check for this payer."
            />
          )
        ) : trackerVisible.length ? (
          <Table>
            <THead>
              <tr>
                <Th>Date</Th>
                <Th>Check</Th>
                <Th>Description</Th>
                <Th>Type</Th>
                <Th>Amount</Th>
              </tr>
            </THead>
            <tbody>
              {(slice as TrackerRow[]).map((row) => (
                <Tr key={row.row_id}>
                  <Td>{fmtDate(row.txn_date)}</Td>
                  <Td>{row.check_number || '—'}</Td>
                  <Td className="max-w-md truncate">{row.description || '—'}</Td>
                  <Td>{row.transaction_type || '—'}</Td>
                  <Td>{money(row.amount)}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <EmptyState
            title={tracker.length ? 'No tracker checks in this view' : 'No tracker checks missing from Waystar'}
            description={
              tracker.length
                ? 'Try another check number.'
                : 'Every tracker check or EFT number is on a Waystar remit.'
            }
          />
        )}
        {!loading && !error && activeRows.length > PAGE_SIZE && (
          <Pagination page={safePage} pages={pages} onPage={setPage} />
        )}
      </TableCard>
    </div>
  )
}
