import { useEffect, useRef, useState } from 'react'
import { forecastApi, money, type Filters } from '../../api/forecast'
import { EmrPatientLink } from '../../components/EmrPatientLink'
import { EmptyState, Table, TableCard, Td, Th, THead, Tr } from '../../components/table'
import { Input } from '../../components/ui'

export function OverdueClaims({
  filters,
  onChangeFilters,
  onError,
}: {
  filters: Filters
  onChangeFilters: (filters: Filters) => void
  onError: (message: string) => void
}) {
  const [overdue, setOverdue] = useState<Awaited<ReturnType<typeof forecastApi.overdueClaims>>>([])
  const sig = JSON.stringify(filters)
  const prev = useRef('')

  useEffect(() => {
    const changed = prev.current !== '' && prev.current !== sig
    prev.current = sig
    let cancelled = false
    const handle = window.setTimeout(() => {
      ;(async () => {
        try {
          onError('')
          const rows = await forecastApi.overdueClaims(filters)
          if (!cancelled) setOverdue(rows)
        } catch (e) {
          if (!cancelled) onError(String((e as Error).message || e))
        }
      })()
    }, changed ? 250 : 0)
    return () => {
      cancelled = true
      window.clearTimeout(handle)
    }
  }, [filters, onError, sig])

  return (
    <TableCard
      title="Overdue claims"
      count={overdue.length}
      countLabel="claims"
      description="Past Insurance-behavior expected land date — not the audit queue."
    >
      <div className="mb-3 max-w-sm">
        <Input
          placeholder="Search patient, EMR, or insurance"
          value={filters.q}
          onChange={(e) => onChangeFilters({ ...filters, q: e.target.value })}
        />
      </div>
      {overdue.length ? (
        <Table sticky>
          <THead>
            <tr>
              <Th>Patient</Th>
              <Th>EMR</Th>
              <Th>DOS</Th>
              <Th>Clinic</Th>
              <Th>Insurance</Th>
              <Th>CPT</Th>
              <Th>Expected</Th>
              <Th>Land date</Th>
              <Th>Overdue days</Th>
              <Th>SLA lag</Th>
            </tr>
          </THead>
          <tbody>
            {overdue.map((row, i) => (
              <Tr key={`${row.emr_patient_id}-${row.dos}-${row.cpt_code}-${i}`}>
                <Td>{row.patient_name || '—'}</Td>
                <Td>
                  <EmrPatientLink id={row.emr_patient_id} facilityName={row.facility_name} />
                </Td>
                <Td>{row.dos || '—'}</Td>
                <Td>{row.facility_name || '—'}</Td>
                <Td>{row.ins_name || '—'}</Td>
                <Td>{row.cpt_code || '—'}</Td>
                <Td>{money(row.expected_amount)}</Td>
                <Td>{row.expected_land_date || '—'}</Td>
                <Td>{row.overdue_days}</Td>
                <Td>{row.sla_lag_days == null ? '—' : row.sla_lag_days}</Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      ) : (
        <EmptyState title="No overdue claims" description="No forecast claims past Insurance-behavior land date for the current filters." />
      )}
    </TableCard>
  )
}
