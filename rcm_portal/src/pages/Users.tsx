import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Plus } from 'lucide-react'
import { api } from '../api/client'
import { useAuth } from '../auth/AuthContext'
import { Alert, Badge, Button, Drawer, Field, Input, PageHeader } from '../components/ui'
import {
  Avatar,
  EmptyState,
  FilterBar,
  Table,
  TableCard,
  Td,
  Th,
  THead,
  Tr,
} from '../components/table'

type UserRow = {
  user_id: string
  username: string
  display_name: string
  email?: string | null
  is_active: boolean
  roles: string[]
  created_at?: string | null
  last_login_at?: string | null
}

const ROLE_OPTIONS: Array<{ key: string; label: string }> = [
  { key: 'posting_team', label: 'Posting Team' },
  { key: 'collector', label: 'Collector' },
  { key: 'finance', label: 'Finance' },
  { key: 'submission', label: 'Submission' },
  { key: 'second_submission', label: 'Second Submission' },
  { key: 'second_submission_lead', label: 'Second Submission Lead' },
  { key: 'analytics_viewer', label: 'Team Analytics' },
  { key: 'medical_audit', label: 'Medical Audit' },
  { key: 'desk', label: 'Desk' },
  { key: 'red_agent', label: 'Red Agent' },
  { key: 'redteam_leader', label: 'Red Team Leader' },
  { key: 'piu', label: 'PIU' },
  { key: 'client_success', label: 'Client Success' },
  { key: 'product_owner', label: 'Product Owner' },
  { key: 'ops_admin', label: 'Operations Admin' },
  { key: 'sub_admin', label: 'Sub Admin' },
  { key: 'super_admin', label: 'Super Admin' },
]

const ROLE_TONE: Record<string, 'gray' | 'blue' | 'green' | 'amber' | 'purple'> = {
  posting_team: 'blue',
  collector: 'gray',
  finance: 'green',
  super_admin: 'purple',
  sub_admin: 'blue',
  submission: 'amber',
  second_submission: 'amber',
  second_submission_lead: 'purple',
  analytics_viewer: 'blue',
  medical_audit: 'purple',
  desk: 'amber',
  red_agent: 'amber',
  redteam_leader: 'purple',
  piu: 'green',
  client_success: 'blue',
  product_owner: 'purple',
  ops_admin: 'blue',
}

function formatWhen(iso?: string | null) {
  if (!iso) return 'Never'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return 'Never'
  return d.toLocaleString()
}

function RoleCheckboxes({
  value,
  onChange,
  lockedKeys = [],
  hiddenKeys = [],
}: {
  value: string[]
  onChange: (next: string[]) => void
  lockedKeys?: string[]
  hiddenKeys?: string[]
}) {
  return (
    <div className="space-y-2">
      {ROLE_OPTIONS.filter((opt) => !hiddenKeys.includes(opt.key)).map((opt) => {
        const checked = value.includes(opt.key)
        const locked = lockedKeys.includes(opt.key) && checked
        return (
          <label key={opt.key} className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-200">
            <input
              type="checkbox"
              checked={checked}
              disabled={locked}
              onChange={(e) => {
                if (e.target.checked) onChange([...value, opt.key])
                else onChange(value.filter((r) => r !== opt.key))
              }}
            />
            {opt.label}
          </label>
        )
      })}
    </div>
  )
}

export function UsersPage() {
  const { user: me, hasRole } = useAuth()
  const [users, setUsers] = useState<UserRow[]>([])
  const [q, setQ] = useState('')
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<UserRow | null>(null)
  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [roles, setRoles] = useState<string[]>(['posting_team'])
  const [isActive, setIsActive] = useState(true)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  const activeSuperCount = users.filter(
    (u) => u.is_active && u.roles.includes('super_admin'),
  ).length

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    if (!needle) return users
    return users.filter((u) => {
      const hay = `${u.display_name} ${u.username} ${u.email || ''} ${u.roles.join(' ')}`
      return hay.toLowerCase().includes(needle)
    })
  }, [users, q])

  async function load() {
    setUsers(await api<UserRow[]>('/api/auth/users'))
  }

  useEffect(() => {
    void load().catch((e) => setError(String((e as Error).message)))
  }, [])

  function resetForm() {
    setUsername('')
    setDisplayName('')
    setEmail('')
    setPassword('')
    setRoles(['posting_team'])
    setIsActive(true)
    setError('')
  }

  function openCreate() {
    resetForm()
    setEditing(null)
    setCreating(true)
  }

  function openEdit(row: UserRow) {
    setCreating(false)
    setEditing(row)
    setUsername(row.username)
    setDisplayName(row.display_name)
    setEmail(row.email || '')
    setPassword('')
    setRoles(row.roles.length ? [...row.roles] : ['posting_team'])
    setIsActive(row.is_active)
    setError('')
  }

  function closeDrawers() {
    setCreating(false)
    setEditing(null)
    resetForm()
  }

  const editingSelf = Boolean(editing && me && editing.user_id === me.user_id)
  const isLastSuper = Boolean(
    editing &&
      editing.is_active &&
      editing.roles.includes('super_admin') &&
      activeSuperCount <= 1,
  )
  const cannotDeactivate = editingSelf || isLastSuper
  const lockedRoleKeys = isLastSuper ? ['super_admin'] : []
  const hiddenRoleKeys = hasRole('super_admin') ? [] : ['super_admin']
  const canEditUser = (row: UserRow) =>
    hasRole('super_admin') || !row.roles.includes('super_admin')

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setError('')
    if (!roles.length) {
      setError('At least one role is required')
      return
    }
    setSaving(true)
    try {
      await api('/api/auth/users', {
        method: 'POST',
        body: JSON.stringify({
          username,
          display_name: displayName,
          email: email.trim() || username.trim(),
          password,
          roles,
          is_active: isActive,
        }),
      })
      closeDrawers()
      await load()
    } catch (err) {
      setError(String((err as Error).message))
    } finally {
      setSaving(false)
    }
  }

  async function onSave(e: FormEvent) {
    e.preventDefault()
    if (!editing) return
    setError('')
    if (!roles.length) {
      setError('At least one role is required')
      return
    }
    if (!isActive && cannotDeactivate) {
      setError(
        editingSelf
          ? 'You cannot deactivate your own account'
          : 'Cannot deactivate the last super admin',
      )
      return
    }
    const pwd = password.trim()
    if (pwd && pwd.length < 6) {
      setError('Password must be at least 6 characters')
      return
    }
    setSaving(true)
    try {
      const body: Record<string, unknown> = {
        username,
        display_name: displayName,
        email: email.trim() || username.trim(),
        roles,
        is_active: isActive,
      }
      if (pwd) body.password = pwd
      await api(`/api/auth/users/${editing.user_id}`, {
        method: 'PATCH',
        body: JSON.stringify(body),
      })
      closeDrawers()
      await load()
    } catch (err) {
      setError(String((err as Error).message))
    } finally {
      setSaving(false)
    }
  }

  const drawerOpen = creating || Boolean(editing)

  return (
    <div className="space-y-6">
      <PageHeader
        title="Users & Roles"
        description="Create users, reset passwords, and assign portal roles."
        actions={
          <Button type="button" onClick={openCreate}>
            <Plus className="h-4 w-4" />
            Add user
          </Button>
        }
      />

      {error && !drawerOpen && <Alert>{error}</Alert>}

      <TableCard title="Directory" count={filtered.length} countLabel="users">
        <FilterBar
          search={q}
          onSearch={setQ}
          searchPlaceholder="Search name, username, role"
        />
        {filtered.length ? (
          <Table>
            <THead>
              <tr>
                <Th>User</Th>
                <Th>Roles</Th>
                <Th>Active</Th>
                <Th>Last login</Th>
              </tr>
            </THead>
            <tbody>
              {filtered.map((u) => (
                <Tr key={u.user_id} onClick={canEditUser(u) ? () => openEdit(u) : undefined}>
                  <Td>
                    <div className="flex items-center gap-3">
                      <Avatar name={u.display_name} />
                      <div>
                        <div className="font-medium text-gray-900 dark:text-white">
                          {u.display_name}
                        </div>
                        <div className="text-xs text-gray-500">{u.username}</div>
                      </div>
                    </div>
                  </Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      {u.roles.map((r) => (
                        <Badge key={r} tone={ROLE_TONE[r] || 'gray'} dot>
                          {r.replace(/_/g, ' ')}
                        </Badge>
                      ))}
                    </div>
                  </Td>
                  <Td>
                    <Badge tone={u.is_active ? 'green' : 'gray'} dot>
                      {u.is_active ? 'Active' : 'Inactive'}
                    </Badge>
                  </Td>
                  <Td>
                    <span className="text-xs text-gray-500">{formatWhen(u.last_login_at)}</span>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <EmptyState
            title={users.length ? 'No matching users' : 'Invite your first user'}
            description={
              users.length
                ? 'Try a different search.'
                : 'Add team members and assign roles.'
            }
            action={
              users.length ? undefined : (
                <Button type="button" onClick={openCreate}>
                  Add user
                </Button>
              )
            }
          />
        )}
      </TableCard>

      <Drawer
        open={drawerOpen}
        onClose={closeDrawers}
        title={editing ? 'Edit user' : 'Create user'}
      >
        <form className="space-y-4" onSubmit={editing ? onSave : onCreate}>
          <Field label="Username">
            <Input
              placeholder="email or username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              minLength={2}
            />
          </Field>
          <Field label="Display name">
            <Input
              placeholder="Display name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              required
            />
          </Field>
          <Field label="Email">
            <Input
              placeholder="Defaults to username"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </Field>
          <Field label={editing ? 'New password' : 'Password'}>
            <Input
              placeholder={editing ? 'Leave blank to keep current' : 'Password'}
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required={!editing}
              minLength={editing ? undefined : 6}
              autoComplete="new-password"
            />
          </Field>
          <div className="text-sm">
            <span className="mb-1.5 block font-medium text-gray-700 dark:text-gray-300">Roles</span>
            <RoleCheckboxes
              value={roles}
              onChange={setRoles}
              lockedKeys={lockedRoleKeys}
              hiddenKeys={hiddenRoleKeys}
            />
          </div>
          <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-200">
            <input
              type="checkbox"
              checked={isActive}
              disabled={Boolean(editing) && cannotDeactivate && isActive}
              onChange={(e) => setIsActive(e.target.checked)}
            />
            Active
          </label>
          {editing && !isActive && (
            <Alert tone="warning">
              {cannotDeactivate
                ? editingSelf
                  ? 'You cannot deactivate your own account.'
                  : 'Cannot deactivate the last super admin.'
                : 'This user will not be able to sign in.'}
            </Alert>
          )}
          {editing && isLastSuper && (
            <Alert tone="warning">
              This is the last active super admin. That role cannot be removed.
            </Alert>
          )}
          {editing && (
            <p className="text-xs text-gray-500">Last login: {formatWhen(editing.last_login_at)}</p>
          )}
          {error && drawerOpen && <Alert>{error}</Alert>}
          <Button type="submit" disabled={saving}>
            {editing ? 'Save changes' : 'Create user'}
          </Button>
        </form>
      </Drawer>
    </div>
  )
}
