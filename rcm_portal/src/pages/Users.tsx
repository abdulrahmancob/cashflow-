import { useEffect, useState, type FormEvent } from 'react'
import { api } from '../api/client'
import { Button, Card, Input, Select } from '../components/ui'

type UserRow = {
  user_id: string
  username: string
  display_name: string
  email?: string | null
  is_active: boolean
  roles: string[]
}

export function UsersPage() {
  const [users, setUsers] = useState<UserRow[]>([])
  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState('posting_team')
  const [error, setError] = useState('')

  async function load() {
    setUsers(await api<UserRow[]>('/api/auth/users'))
  }

  useEffect(() => {
    void load().catch((e) => setError(String(e.message)))
  }, [])

  async function onCreate(e: FormEvent) {
    e.preventDefault()
    setError('')
    try {
      await api('/api/auth/users', {
        method: 'POST',
        body: JSON.stringify({
          username,
          display_name: displayName,
          password,
          roles: [role],
        }),
      })
      setUsername('')
      setDisplayName('')
      setPassword('')
      await load()
    } catch (err) {
      setError(String((err as Error).message))
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="font-display text-2xl font-semibold text-gray-900 dark:text-white">
          Users & Roles
        </h1>
        <p className="mt-1 text-sm text-gray-500">Super Admin user management.</p>
      </div>

      <Card title="Create user">
        <form className="grid gap-3 md:grid-cols-5" onSubmit={onCreate}>
          <Input
            placeholder="Username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
          />
          <Input
            placeholder="Display name"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            required
          />
          <Input
            placeholder="Password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          <Select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="posting_team">Posting Team</option>
            <option value="finance">Finance</option>
            <option value="super_admin">Super Admin</option>
          </Select>
          <Button type="submit">Create</Button>
        </form>
        {error && <p className="mt-3 text-sm text-rose-600">{error}</p>}
      </Card>

      <Card title="Directory">
        <div className="overflow-x-auto">
          <table className="min-w-full text-left text-sm">
            <thead className="bg-gray-50 text-xs uppercase text-gray-500 dark:bg-gray-800">
              <tr>
                <th className="px-3 py-2">User</th>
                <th className="px-3 py-2">Roles</th>
                <th className="px-3 py-2">Active</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.user_id} className="border-t border-gray-100 dark:border-gray-800">
                  <td className="px-3 py-2">
                    <div className="font-medium">{u.display_name}</div>
                    <div className="text-xs text-gray-500">{u.username}</div>
                  </td>
                  <td className="px-3 py-2">{u.roles.join(', ')}</td>
                  <td className="px-3 py-2">{u.is_active ? 'Yes' : 'No'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}
