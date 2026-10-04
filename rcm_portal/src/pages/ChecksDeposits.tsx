import { useCallback, useEffect, useMemo, useState } from 'react'
import { Download, History, Plus, Search, Shield, Trash2, Upload } from 'lucide-react'
import { ApiError } from '../api/client'
import {
  commitChecksUpload,
  createChecksRow,
  deleteChecksGrant,
  downloadChecksExport,
  fetchChecksGrants,
  fetchChecksMe,
  fetchChecksMonths,
  fetchChecksRowHistory,
  fetchChecksRows,
  patchChecksRow,
  previewChecksUpload,
  putChecksGrant,
  restoreChecksRow,
  softDeleteChecksRow,
  type ChecksPerms,
  type ChecksRow,
  type ChecksUploadPreview,
} from '../api/checksDeposits'
import {
  EmptyState,
  FilterBar,
  Pagination,
  Table,
  TableCard,
  Td,
  Th,
  THead,
  Tr,
} from '../components/table'
import { Alert, Button, Card, Drawer, Input, PageHeader, Select } from '../components/ui'

type Draft = Partial<ChecksRow> & { version?: number }

const EMPTY_DRAFT: Draft = {
  check_number: '',
  check_date: '',
  payer: '',
  amount: '',
  deposit_date: '',
  link: '',
  notes: '',
}

function currentMonth(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

function money(value: number | string | null | undefined) {
  const n = Number(value ?? 0)
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString(undefined, { style: 'currency', currency: 'USD' })
}

export function ChecksDepositsPage() {
  const [perms, setPerms] = useState<ChecksPerms | null>(null)
  const [months, setMonths] = useState<string[]>([])
  const [month, setMonth] = useState(currentMonth())
  const [q, setQ] = useState('')
  const [includeDeleted, setIncludeDeleted] = useState(false)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [amountTotal, setAmountTotal] = useState<number | string>(0)
  const [rows, setRows] = useState<ChecksRow[]>([])
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
  const [preview, setPreview] = useState<ChecksUploadPreview | null>(null)
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
    const data = await fetchChecksRows({
      month,
      q: q || undefined,
      page,
      page_size: pageSize,
      include_deleted: includeDeleted,
    })
    setRows(data.items)
    setTotal(data.total)
    setAmountTotal(data.amount_total)
    setDrafts({})
  }, [month, q, page, includeDeleted])

  useEffect(() => {
    void fetchChecksMe()
      .then(setPerms)
      .catch((e) => setError(String((e as Error).message)))
  }, [])

  useEffect(() => {
    if (!perms?.can_view) return
    void fetchChecksMonths()
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

  async function saveRow(row: ChecksRow) {
    const draft = drafts[row.row_id]
    if (!draft) return
    setBusy(true)
    setError('')
    try {
      await patchChecksRow(row.row_id, { version: row.version, ...draft })
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
    if (!newRow.deposit_date || newRow.amount === '' || newRow.amount == null) {
      setError('Deposit date and amount are required')
      return
    }
    setBusy(true)
    setError('')
    try {
      await createChecksRow({
        ...newRow,
        amount: Number(newRow.amount),
        check_date: newRow.check_date || null,
        deposit_date: String(newRow.deposit_date),
      })
      setNewRow({ ...EMPTY_DRAFT })
      setShowAdd(false)
      await load()
      const m = await fetchChecksMonths()
      setMonths(m.months)
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  async function onDelete(row: ChecksRow) {
    if (!confirm(`Soft-delete check ${row.check_number || row.sheet_key}?`)) return
    setBusy(true)
    try {
      await softDeleteChecksRow(row.row_id, row.version)
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

  async function onRestore(row: ChecksRow) {
    setBusy(true)
    try {
      await restoreChecksRow(row.row_id, row.version)
      await load()
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  async function openHistory(rowId: string) {
    setHistoryFor(rowId)
    const data = await fetchChecksRowHistory(rowId)
    setHistory(data.items)
  }

  async function onUpload(file: File | null) {
    if (!file) return
    setBusy(true)
    setError('')
    try {
      setPreview(await previewChecksUpload(file))
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
      await commitChecksUpload(preview.preview_id)
      setPreview(null)
      await load()
      const m = await fetchChecksMonths()
      setMonths(m.months)
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  async function openAccess() {
    setShowAccess(true)
    const data = await fetchChecksGrants()
    setGrants(data.grants)
    setAllUsers(data.users)
  }

  async function saveGrant() {
    if (!grantUserId) return
    setBusy(true)
    try {
      await putChecksGrant(grantUserId, grantFlags)
      await openAccess()
    } catch (e) {
      setError(String((e as Error).message))
    } finally {
      setBusy(false)
    }
  }

  if (!perms) {
    return (
      <div className="space-y-4">
        <PageHeader title="Checks & Deposits" description="Loading permissions…" />
      </div>
    )
  }
  if (!perms.can_view) {
    return (
      <div className="space-y-4">
        <PageHeader title="Checks & Deposits" />
        <Alert>
          You do not have access to Checks & Deposits. Ask an admin to grant view permission.
        </Alert>
      </div>
    )
  }

  function cell(row: ChecksRow, field: keyof ChecksRow, type: 'text' | 'date' | 'number' = 'text') {
    const draft = drafts[row.row_id]
    const value = draft && field in draft ? draft[field] : row[field]
    const disabled = !perms?.can_edit || Boolean(row.deleted_at)
    return (
      <input
        className="w-full min-w-[6rem] rounded-md border border-transparent bg-transparent px-1.5 py-1 text-[13px] text-gray-800 hover:bg-gray-50 focus:border-brand-400 focus:bg-white focus:ring-4 focus:ring-brand-500/15 dark:text-gray-100 dark:hover:bg-gray-800 dark:focus:bg-gray-950"
        type={type === 'number' ? 'number' : type === 'date' ? 'date' : 'text'}
        disabled={disabled}
        value={
          value == null
            ? ''
            : type === 'date'
              ? String(value).slice(0, 10)
              : String(value)
        }
        onChange={(e) => updateDraft(row.row_id, field, e.target.value)}
      />
    )
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Checks & Deposits"
        description="Cashed-check ledger for 2026 deposit dates — filter by month, edit rows, upload or download the workbook."
        actions={
          <>
            {perms.can_admin && (
              <Button type="button" variant="secondary" onClick={() => void openAccess()}>
                <Shield className="h-4 w-4" />
                Access
              </Button>
            )}
            {perms.can_view && (
              <Button
                type="button"
                variant="secondary"
                disabled={busy}
                onClick={() =>
                  void downloadChecksExport(month).catch((e) => setError(String(e.message)))
                }
              >
                <Download className="h-4 w-4" />
                Download
              </Button>
            )}
            {perms.can_upload && (
              <label className="inline-flex cursor-pointer items-center">
                <span className="inline-flex h-10 items-center gap-2 rounded-lg bg-brand-600 px-3.5 text-sm font-semibold text-white shadow-xs hover:bg-brand-700">
                  <Upload className="h-4 w-4" />
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
                <Plus className="h-4 w-4" />
                {showAdd ? 'Cancel' : 'Add row'}
              </Button>
            )}
          </>
        }
      />

      {error && <Alert>{error}</Alert>}

      {showAdd && perms.can_edit && (
        <Card title="New check">
          <div className="grid gap-2 md:grid-cols-4">
            {(
              [
                ['check_number', 'Check number'],
                ['check_date', 'Check date'],
                ['payer', 'Payer'],
                ['amount', 'Amount'],
                ['deposit_date', 'Deposit date'],
                ['link', 'Link'],
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

      <TableCard
        title="2026 deposits"
        count={total}
        countLabel="checks"
        description={`Month ${month} · ${money(amountTotal)}`}
      >
        <FilterBar
          extra={
            <>
              <Select
                className="w-36"
                value={month}
                onChange={(e) => {
                  setPage(1)
                  setMonth(e.target.value)
                }}
              >
                {monthOptions.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </Select>
              <Input
                icon={<Search className="h-4 w-4" />}
                className="w-56"
                value={q}
                placeholder="Check #, payer, link…"
                onChange={(e) => setQ(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    setPage(1)
                    void load()
                  }
                }}
              />
              <Button
                type="button"
                variant="secondary"
                size="sm"
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
                    className="rounded border-gray-300 text-brand-600"
                    checked={includeDeleted}
                    onChange={(e) => {
                      setIncludeDeleted(e.target.checked)
                      setPage(1)
                    }}
                  />
                  Show deleted
                </label>
              )}
            </>
          }
        />

        {rows.length ? (
          <Table sticky dense>
            <THead>
              <tr>
                {['Check #', 'Check date', 'Payer', 'Amount', 'Deposit date', 'Link', 'Notes', ''].map(
                  (h) => (
                    <Th key={h || 'actions'}>{h}</Th>
                  ),
                )}
              </tr>
            </THead>
            <tbody>
              {rows.map((row) => {
                const dirty = Boolean(drafts[row.row_id])
                return (
                  <Tr
                    key={row.row_id}
                    className={row.deleted_at ? 'bg-rose-50/50 opacity-70 dark:bg-rose-950/20' : ''}
                  >
                    <Td className="font-medium">{cell(row, 'check_number')}</Td>
                    <Td>{cell(row, 'check_date', 'date')}</Td>
                    <Td>{cell(row, 'payer')}</Td>
                    <Td>{cell(row, 'amount', 'number')}</Td>
                    <Td>{cell(row, 'deposit_date', 'date')}</Td>
                    <Td>{cell(row, 'link')}</Td>
                    <Td>{cell(row, 'notes')}</Td>
                    <Td>
                      <div className="flex gap-1">
                        {perms.can_edit && dirty && !row.deleted_at && (
                          <Button
                            type="button"
                            variant="secondary"
                            size="sm"
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
                            size="icon"
                            className="h-8 w-8"
                            disabled={busy}
                            onClick={() => void onDelete(row)}
                            aria-label="Delete"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        )}
                        {perms.can_edit && row.deleted_at && (
                          <Button
                            type="button"
                            variant="secondary"
                            size="sm"
                            disabled={busy}
                            onClick={() => void onRestore(row)}
                          >
                            Restore
                          </Button>
                        )}
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8"
                          onClick={() => void openHistory(row.row_id)}
                          aria-label="History"
                        >
                          <History className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </Td>
                  </Tr>
                )
              })}
            </tbody>
          </Table>
        ) : (
          <EmptyState
            title="No checks found"
            description="No 2026 rows for this filter. Try another month or clear search."
            icon={<Search className="h-6 w-6" />}
          />
        )}
        <Pagination page={page} pages={pageCount} onPage={setPage} />
      </TableCard>

      <Drawer open={!!historyFor} onClose={() => setHistoryFor(null)} title="Audit history">
        <ul className="space-y-3 text-sm">
          {history.map((h) => (
            <li key={h.audit_id} className="rounded-lg border border-gray-200 p-3 dark:border-gray-800">
              <div className="font-medium">{h.action}</div>
              <div className="text-xs text-gray-500">
                {h.acted_at} · {h.actor_display_name || h.actor_username || 'system'}
              </div>
            </li>
          ))}
          {!history.length && <li className="text-gray-500">No history.</li>}
        </ul>
      </Drawer>

      {preview && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4">
          <div className="max-h-[90vh] w-full max-w-lg overflow-auto rounded-2xl bg-white p-5 shadow-xl dark:bg-gray-900">
            <h2 className="font-display text-lg font-semibold">Upload preview</h2>
            <p className="mt-1 text-sm text-gray-500">{preview.filename}</p>
            <ul className="mt-4 space-y-1 text-sm">
              <li>Parsed 2026 rows: {preview.parsed_rows}</li>
              <li>Adds: {preview.counts.adds}</li>
              <li>Updates: {preview.counts.updates}</li>
              <li>Unchanged: {preview.counts.unchanged}</li>
              <li>Soft-deletes: {preview.counts.soft_deletes}</li>
              <li>Skipped outside 2026: {preview.skipped_out_of_year}</li>
              <li>Skipped, no amount: {preview.skipped_no_amount}</li>
              <li>Duplicate check keys: {preview.duplicate_sheet_keys}</li>
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
              <h2 className="font-display text-lg font-semibold">Checks & Deposits access</h2>
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
                    <td className="py-2">{g.display_name || g.username}</td>
                    <td>{g.can_view ? '✓' : ''}</td>
                    <td>{g.can_edit ? '✓' : ''}</td>
                    <td>{g.can_upload ? '✓' : ''}</td>
                    <td>{g.can_admin ? '✓' : ''}</td>
                    <td>
                      <Button
                        type="button"
                        variant="ghost"
                        onClick={() =>
                          void deleteChecksGrant(g.user_id)
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
