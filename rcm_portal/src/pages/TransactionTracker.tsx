import { useCallback, useEffect, useMemo, useState } from 'react'
import { ApiError } from '../api/client'
import {
  commitTrackerUpload,
  createTrackerRow,
  deleteTrackerGrant,
  downloadTrackerExport,
  fetchRowHistory,
  fetchTrackerGrants,
  fetchTrackerMe,
  fetchTrackerMonths,
  fetchTrackerRows,
  patchTrackerRow,
  previewTrackerUpload,
  putTrackerGrant,
  restoreTrackerRow,
  softDeleteTrackerRow,
  type TrackerPerms,
  type TrackerRow,
  type UploadPreview,
} from '../api/tracker'
import { Button, Card, Input, Select } from '../components/ui'

type Draft = Partial<TrackerRow> & { version?: number }

const EMPTY_DRAFT: Draft = {
  payment_id: '',
  txn_date: '',
  month_date: '',
  amount: '',
  eft_1: '',
  eft_2: '',
  transaction_type: '',
  description: '',
  check_reference: '',
  bank_name: '',
  billing_status: '',
  collector: '',
  posted: false,
  notes: '',
  assigned_date: '',
  claims: '',
}

function currentMonth(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

export function TransactionTrackerPage() {
  const [perms, setPerms] = useState<TrackerPerms | null>(null)
  const [months, setMonths] = useState<string[]>([])
  const [month, setMonth] = useState(currentMonth())
  const [q, setQ] = useState('')
  const [includeDeleted, setIncludeDeleted] = useState(false)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [rows, setRows] = useState<TrackerRow[]>([])
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [newRow, setNewRow] = useState<Draft>({ ...EMPTY_DRAFT })
  const [showAdd, setShowAdd] = useState(false)
  const [historyFor, setHistoryFor] = useState<string | null>(null)
  const [history, setHistory] = useState<
    Array<{
      audit_id: string
      action: string
      acted_at: string
      actor_display_name?: string
      actor_username?: string
    }>
  >([])
  const [preview, setPreview] = useState<UploadPreview | null>(null)
  const [showAccess, setShowAccess] = useState(false)
  const [grants, setGrants] = useState<
    Array<{
      user_id: string
      username?: string
      display_name?: string
      can_view: boolean
      can_edit: boolean
      can_upload: boolean
      can_admin: boolean
    }>
  >([])
  const [allUsers, setAllUsers] = useState<
    Array<{ user_id: string; username: string; display_name: string; roles: string[] }>
  >([])
  const [grantUserId, setGrantUserId] = useState('')
  const [grantFlags, setGrantFlags] = useState({
    can_view: true,
    can_edit: false,
    can_upload: false,
    can_admin: false,
  })

  const pageSize = 50
  const pageCount = Math.max(1, Math.ceil(total / pageSize))

  const load = useCallback(async () => {
    setError('')
    const data = await fetchTrackerRows({
      month,
      q: q || undefined,
      page,
      page_size: pageSize,
      include_deleted: includeDeleted,
    })
    setRows(data.items)
    setTotal(data.total)
    setDrafts({})
  }, [month, q, page, includeDeleted])

  useEffect(() => {
    void fetchTrackerMe()
      .then(setPerms)
      .catch((e) => setError(String((e as Error).message)))
  }, [])

  useEffect(() => {
    if (!perms?.can_view) return
    void fetchTrackerMonths()
      .then((m) => {
        setMonths(m.months)
        if (m.months.length && !m.months.includes(month)) {
          setMonth(m.months[0])
        }
      })
      .catch(() => undefined)
  }, [perms?.can_view]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!perms?.can_view) return
    void load().catch((e) => setError(String((e as Error).message)))
  }, [perms?.can_view, load])

  const monthOptions = useMemo(() => {
    const set = new Set(months)
    set.add(month)
    return Array.from(set).sort().reverse()
  }, [months, month])

  function updateDraft(rowId: string, field: string, value: unknown) {
    setDrafts((prev) => ({
      ...prev,
      [rowId]: { ...(prev[rowId] || {}), [field]: value },
    }))
  }

  async function saveRow(row: TrackerRow) {
    const draft = drafts[row.row_id]
    if (!draft) return
    setBusy(true)
    setError('')
    try {
      const body: Record<string, unknown> = { version: row.version, ...draft }
      await patchTrackerRow(row.row_id, body)
      await load()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setError('Version conflict — row was updated elsewhere. Reloaded.')
        await load()
      } else {
        setError(String((e as Error).message))
      }
    } finally {
      setBusy(false)
    }
  }

  async function onAdd() {
    if (!newRow.payment_id || !newRow.txn_date || newRow.amount === '' || newRow.amount == null) {
      setError('Payment ID, Date, and Amount are required')
      return
    }
    setBusy(true)
    setError('')
    try {
      const txn = String(newRow.txn_date)
      await createTrackerRow({
        ...newRow,
        txn_date: txn,
        month_date: newRow.month_date || `${txn.slice(0, 8)}01`,
        amount: Number(newRow.amount),
        posted: Boolean(newRow.posted),
      })
      setNewRow({ ...EMPTY_DRAFT })
      setShowAdd(false)
      await load()
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  async function onDelete(row: TrackerRow) {
    if (!confirm(`Soft-delete ${row.payment_id}?`)) return
    setBusy(true)
    try {
      await softDeleteTrackerRow(row.row_id, row.version)
      await load()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setError('Version conflict — reloaded.')
        await load()
      } else setError(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  async function onRestore(row: TrackerRow) {
    setBusy(true)
    try {
      await restoreTrackerRow(row.row_id, row.version)
      await load()
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  async function openHistory(rowId: string) {
    setHistoryFor(rowId)
    const data = await fetchRowHistory(rowId)
    setHistory(data.items)
  }

  async function onUpload(file: File | null) {
    if (!file) return
    setBusy(true)
    setError('')
    try {
      setPreview(await previewTrackerUpload(file))
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  async function onCommitPreview() {
    if (!preview) return
    setBusy(true)
    try {
      await commitTrackerUpload(preview.preview_id)
      setPreview(null)
      await load()
      const m = await fetchTrackerMonths()
      setMonths(m.months)
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  async function openAccess() {
    setShowAccess(true)
    const data = await fetchTrackerGrants()
    setGrants(data.grants)
    setAllUsers(data.users)
  }

  async function saveGrant() {
    if (!grantUserId) return
    setBusy(true)
    try {
      await putTrackerGrant(grantUserId, grantFlags)
      await openAccess()
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  if (!perms) {
    return <div className="text-sm text-gray-500">Loading permissions…</div>
  }
  if (!perms.can_view) {
    return (
      <div className="text-sm text-rose-600">
        You do not have access to the Transaction Tracker. Ask an admin to grant view permission.
      </div>
    )
  }

  function cell(row: TrackerRow, field: keyof TrackerRow, type: 'text' | 'date' | 'number' | 'check' = 'text') {
    const draft = drafts[row.row_id]
    const value = draft && field in draft ? draft[field] : row[field]
    const disabled = !perms?.can_edit || Boolean(row.deleted_at)
    if (type === 'check') {
      return (
        <input
          type="checkbox"
          disabled={disabled}
          checked={Boolean(value)}
          onChange={(e) => updateDraft(row.row_id, field, e.target.checked)}
        />
      )
    }
    return (
      <input
        className="w-full min-w-[6rem] rounded border border-transparent bg-transparent px-1 py-0.5 text-xs focus:border-brand-400 focus:bg-white dark:focus:bg-gray-950"
        type={type === 'number' ? 'number' : type === 'date' ? 'date' : 'text'}
        disabled={disabled}
        value={value == null ? '' : String(value).slice(0, 10) === String(value) && type === 'date' ? String(value).slice(0, 10) : String(value)}
        onChange={(e) =>
          updateDraft(
            row.row_id,
            field,
            type === 'number' ? e.target.value : e.target.value,
          )
        }
      />
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-display text-2xl font-semibold text-gray-900 dark:text-white">
            Transaction Tracker
          </h1>
          <p className="mt-1 text-sm text-gray-500">
            Bank deposit ledger — filter by month, edit rows, upload/download workbook.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {perms.can_admin && (
            <Button type="button" variant="secondary" onClick={() => void openAccess()}>
              Access
            </Button>
          )}
          {perms.can_view && (
            <Button
              type="button"
              variant="secondary"
              disabled={busy}
              onClick={() => void downloadTrackerExport(month).catch((e) => setError(String(e.message)))}
            >
              Download
            </Button>
          )}
          {perms.can_upload && (
            <label className="inline-flex cursor-pointer items-center">
              <span className="rounded-lg bg-brand-600 px-3 py-2 text-sm font-medium text-white hover:bg-brand-700">
                Upload
              </span>
              <input
                type="file"
                accept=".xlsx,.xlsm"
                className="hidden"
                onChange={(e) => void onUpload(e.target.files?.[0] || null)}
              />
            </label>
          )}
          {perms.can_edit && (
            <Button type="button" onClick={() => setShowAdd((v) => !v)}>
              {showAdd ? 'Cancel' : 'Add row'}
            </Button>
          )}
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-200">
          {error}
        </div>
      )}

      <Card>
        <div className="flex flex-wrap items-end gap-3">
          <label className="text-sm">
            <span className="mb-1 block text-xs text-gray-500">Month</span>
            <Select value={month} onChange={(e) => { setPage(1); setMonth(e.target.value) }}>
              {monthOptions.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </Select>
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-xs text-gray-500">Search</span>
            <Input
              value={q}
              placeholder="Payment ID, EFT, bank…"
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  setPage(1)
                  void load()
                }
              }}
            />
          </label>
          <Button
            type="button"
            variant="secondary"
            onClick={() => {
              setPage(1)
              void load()
            }}
          >
            Apply
          </Button>
          {perms.can_edit && (
            <label className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-300">
              <input
                type="checkbox"
                checked={includeDeleted}
                onChange={(e) => {
                  setIncludeDeleted(e.target.checked)
                  setPage(1)
                }}
              />
              Show deleted
            </label>
          )}
          <div className="ml-auto text-xs text-gray-500">
            {total} rows · page {page}/{pageCount}
          </div>
        </div>
      </Card>

      {showAdd && perms.can_edit && (
        <Card title="New row">
          <div className="grid gap-2 md:grid-cols-4">
            {(
              [
                ['payment_id', 'Payment ID'],
                ['txn_date', 'Date'],
                ['month_date', 'Month'],
                ['amount', 'Amount'],
                ['eft_1', 'EFT_1'],
                ['eft_2', 'EFT_2'],
                ['transaction_type', 'Type'],
                ['bank_name', 'Bank'],
                ['billing_status', 'Billing Status'],
                ['collector', 'Collector'],
                ['check_reference', 'Check/Ref'],
                ['assigned_date', 'Assigned date'],
              ] as const
            ).map(([key, label]) => (
              <label key={key} className="text-xs">
                <span className="text-gray-500">{label}</span>
                <Input
                  type={key.includes('date') ? 'date' : key === 'amount' ? 'number' : 'text'}
                  value={String(newRow[key] ?? '')}
                  onChange={(e) => setNewRow((r) => ({ ...r, [key]: e.target.value }))}
                />
              </label>
            ))}
            <label className="text-xs md:col-span-2">
              <span className="text-gray-500">Description</span>
              <Input
                value={String(newRow.description ?? '')}
                onChange={(e) => setNewRow((r) => ({ ...r, description: e.target.value }))}
              />
            </label>
            <label className="text-xs md:col-span-2">
              <span className="text-gray-500">Notes</span>
              <Input
                value={String(newRow.notes ?? '')}
                onChange={(e) => setNewRow((r) => ({ ...r, notes: e.target.value }))}
              />
            </label>
          </div>
          <div className="mt-3">
            <Button type="button" disabled={busy} onClick={() => void onAdd()}>
              Save new row
            </Button>
          </div>
        </Card>
      )}

      <Card>
        <div className="overflow-x-auto">
          <table className="min-w-full text-left text-xs">
            <thead className="bg-gray-50 text-[10px] uppercase text-gray-500 dark:bg-gray-800">
              <tr>
                {[
                  'Payment ID',
                  'Date',
                  'Amount',
                  'EFT_1',
                  'EFT_2',
                  'Type',
                  'Bank',
                  'Status',
                  'Collector',
                  'Posted',
                  'Notes',
                  '',
                ].map((h) => (
                  <th key={h || 'actions'} className="whitespace-nowrap px-2 py-2">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
              {rows.map((row) => {
                const dirty = Boolean(drafts[row.row_id])
                return (
                  <tr
                    key={row.row_id}
                    className={row.deleted_at ? 'bg-rose-50/50 opacity-70 dark:bg-rose-950/20' : ''}
                  >
                    <td className="px-2 py-1 font-medium">{cell(row, 'payment_id')}</td>
                    <td className="px-2 py-1">{cell(row, 'txn_date', 'date')}</td>
                    <td className="px-2 py-1">{cell(row, 'amount', 'number')}</td>
                    <td className="px-2 py-1">{cell(row, 'eft_1')}</td>
                    <td className="px-2 py-1">{cell(row, 'eft_2')}</td>
                    <td className="px-2 py-1">{cell(row, 'transaction_type')}</td>
                    <td className="px-2 py-1">{cell(row, 'bank_name')}</td>
                    <td className="px-2 py-1">{cell(row, 'billing_status')}</td>
                    <td className="px-2 py-1">{cell(row, 'collector')}</td>
                    <td className="px-2 py-1 text-center">{cell(row, 'posted', 'check')}</td>
                    <td className="px-2 py-1">{cell(row, 'notes')}</td>
                    <td className="whitespace-nowrap px-2 py-1">
                      <div className="flex gap-1">
                        {perms.can_edit && dirty && !row.deleted_at && (
                          <Button
                            type="button"
                            variant="secondary"
                            disabled={busy}
                            onClick={() => void saveRow(row)}
                          >
                            Save
                          </Button>
                        )}
                        {perms.can_edit && !row.deleted_at && (
                          <Button
                            type="button"
                            variant="ghost"
                            disabled={busy}
                            onClick={() => void onDelete(row)}
                          >
                            Del
                          </Button>
                        )}
                        {perms.can_edit && row.deleted_at && (
                          <Button
                            type="button"
                            variant="secondary"
                            disabled={busy}
                            onClick={() => void onRestore(row)}
                          >
                            Restore
                          </Button>
                        )}
                        <Button
                          type="button"
                          variant="ghost"
                          onClick={() => void openHistory(row.row_id)}
                        >
                          Hist
                        </Button>
                      </div>
                    </td>
                  </tr>
                )
              })}
              {!rows.length && (
                <tr>
                  <td colSpan={12} className="px-3 py-8 text-center text-sm text-gray-500">
                    No rows for this filter.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="mt-3 flex gap-2">
          <Button
            type="button"
            variant="secondary"
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
          >
            Prev
          </Button>
          <Button
            type="button"
            variant="secondary"
            disabled={page >= pageCount}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
          </Button>
        </div>
      </Card>

      {historyFor && (
        <div className="fixed inset-0 z-40 flex justify-end bg-black/30" onClick={() => setHistoryFor(null)}>
          <div
            className="h-full w-full max-w-md overflow-auto bg-white p-5 shadow-xl dark:bg-gray-900"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-4 flex items-center justify-between">
              <h2 className="font-display text-lg font-semibold">Audit history</h2>
              <Button type="button" variant="ghost" onClick={() => setHistoryFor(null)}>
                Close
              </Button>
            </div>
            <ul className="space-y-3 text-sm">
              {history.map((h) => (
                <li key={h.audit_id} className="rounded-lg border border-gray-100 p-3 dark:border-gray-800">
                  <div className="font-medium">{h.action}</div>
                  <div className="text-xs text-gray-500">
                    {h.acted_at} · {h.actor_display_name || h.actor_username || 'system'}
                  </div>
                </li>
              ))}
              {!history.length && <li className="text-gray-500">No history.</li>}
            </ul>
          </div>
        </div>
      )}

      {preview && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4">
          <div className="max-h-[90vh] w-full max-w-lg overflow-auto rounded-2xl bg-white p-5 shadow-xl dark:bg-gray-900">
            <h2 className="font-display text-lg font-semibold">Upload preview</h2>
            <p className="mt-1 text-sm text-gray-500">{preview.filename}</p>
            <ul className="mt-4 space-y-1 text-sm">
              <li>Parsed rows: {preview.parsed_rows}</li>
              <li>Adds: {preview.counts.adds}</li>
              <li>Updates: {preview.counts.updates}</li>
              <li>Unchanged: {preview.counts.unchanged}</li>
              <li>Soft-deletes: {preview.counts.soft_deletes}</li>
              <li>Parse errors: {preview.error_count}</li>
            </ul>
            {preview.errors_sample?.length > 0 && (
              <div className="mt-3 max-h-32 overflow-auto rounded border border-amber-200 bg-amber-50 p-2 text-xs dark:border-amber-900 dark:bg-amber-950/30">
                {preview.errors_sample.slice(0, 15).map((e, i) => (
                  <div key={i}>
                    {e.sheet}
                    {e.row != null ? ` #${e.row}` : ''}: {e.message}
                  </div>
                ))}
              </div>
            )}
            <div className="mt-5 flex justify-end gap-2">
              <Button type="button" variant="secondary" onClick={() => setPreview(null)}>
                Cancel
              </Button>
              <Button type="button" disabled={busy} onClick={() => void onCommitPreview()}>
                Commit upload
              </Button>
            </div>
          </div>
        </div>
      )}

      {showAccess && perms.can_admin && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4">
          <div className="max-h-[90vh] w-full max-w-2xl overflow-auto rounded-2xl bg-white p-5 shadow-xl dark:bg-gray-900">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="font-display text-lg font-semibold">Tracker access</h2>
              <Button type="button" variant="ghost" onClick={() => setShowAccess(false)}>
                Close
              </Button>
            </div>
            <div className="grid gap-2 md:grid-cols-2">
              <label className="text-sm md:col-span-2">
                <span className="text-xs text-gray-500">User (any team)</span>
                <Select value={grantUserId} onChange={(e) => setGrantUserId(e.target.value)}>
                  <option value="">Select user…</option>
                  {allUsers.map((u) => (
                    <option key={u.user_id} value={u.user_id}>
                      {u.display_name} ({u.username}) — {(u.roles || []).join(', ')}
                    </option>
                  ))}
                </Select>
              </label>
              {(
                [
                  ['can_view', 'View'],
                  ['can_edit', 'Edit'],
                  ['can_upload', 'Upload'],
                  ['can_admin', 'Admin'],
                ] as const
              ).map(([key, label]) => (
                <label key={key} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={grantFlags[key]}
                    onChange={(e) =>
                      setGrantFlags((f) => ({
                        ...f,
                        [key]: e.target.checked,
                        ...(key !== 'can_view' && e.target.checked ? { can_view: true } : {}),
                      }))
                    }
                  />
                  {label}
                </label>
              ))}
            </div>
            <div className="mt-3">
              <Button type="button" disabled={busy || !grantUserId} onClick={() => void saveGrant()}>
                Save grant
              </Button>
            </div>
            <table className="mt-6 min-w-full text-left text-sm">
              <thead className="text-xs uppercase text-gray-500">
                <tr>
                  <th className="py-2">User</th>
                  <th>V</th>
                  <th>E</th>
                  <th>U</th>
                  <th>A</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {grants.map((g) => (
                  <tr key={g.user_id} className="border-t border-gray-100 dark:border-gray-800">
                    <td className="py-2">
                      {g.display_name || g.username}
                    </td>
                    <td>{g.can_view ? '✓' : ''}</td>
                    <td>{g.can_edit ? '✓' : ''}</td>
                    <td>{g.can_upload ? '✓' : ''}</td>
                    <td>{g.can_admin ? '✓' : ''}</td>
                    <td>
                      <Button
                        type="button"
                        variant="ghost"
                        onClick={() =>
                          void deleteTrackerGrant(g.user_id)
                            .then(openAccess)
                            .catch((e) => setError(String(e.message)))
                        }
                      >
                        Revoke
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
