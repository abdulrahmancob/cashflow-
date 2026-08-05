import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useEffect, useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { Button } from './ui'

type NavItem = {
  to: string
  label: string
  roles?: Array<'super_admin' | 'finance' | 'posting_team'>
  trackerView?: boolean
}

const NAV: NavItem[] = [
  { to: '/eligibility', label: 'Eligibility Queue', roles: ['posting_team', 'super_admin'] },
  { to: '/tracker', label: 'Transaction Tracker', trackerView: true },
  { to: '/finance/mission', label: 'Mission Control', roles: ['finance', 'super_admin'] },
  { to: '/finance/cash', label: 'Cash Trajectory', roles: ['finance', 'super_admin'] },
  { to: '/finance/insights', label: 'Business Insights', roles: ['finance', 'super_admin'] },
  { to: '/finance/drill', label: 'Drill Decks', roles: ['finance', 'super_admin'] },
  { to: '/platform', label: 'Platform Health', roles: ['super_admin'] },
  { to: '/users', label: 'Users', roles: ['super_admin'] },
]

export function Layout() {
  const { user, logout, hasRole, canTracker } = useAuth()
  const navigate = useNavigate()
  const [dark, setDark] = useState(() => localStorage.getItem('rcm_theme') === 'dark')
  const [open, setOpen] = useState(true)

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    localStorage.setItem('rcm_theme', dark ? 'dark' : 'light')
  }, [dark])

  const items = NAV.filter((n) => {
    if (n.trackerView) return canTracker('view')
    return n.roles ? hasRole(...n.roles) : false
  })

  return (
    <div className="flex min-h-full bg-gray-50 dark:bg-gray-950">
      <aside
        className={`${
          open ? 'w-64' : 'w-0 overflow-hidden'
        } shrink-0 border-r border-gray-200 bg-white transition-all dark:border-gray-800 dark:bg-gray-900`}
      >
        <div className="flex h-16 items-center gap-2 border-b border-gray-100 px-5 dark:border-gray-800">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-brand-600 font-display text-sm font-bold text-white">
            RCM
          </div>
          <div>
            <div className="font-display text-sm font-semibold text-gray-900 dark:text-white">
              Operations Portal
            </div>
            <div className="text-xs text-gray-500">PT of the City</div>
          </div>
        </div>
        <nav className="space-y-1 p-3">
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `block rounded-lg px-3 py-2.5 text-sm font-medium transition ${
                  isActive
                    ? 'bg-brand-50 text-brand-700 dark:bg-brand-700/20 dark:text-brand-100'
                    : 'text-gray-600 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800'
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-16 items-center justify-between border-b border-gray-200 bg-white px-4 dark:border-gray-800 dark:bg-gray-900">
          <div className="flex items-center gap-2">
            <Button variant="ghost" type="button" onClick={() => setOpen((v) => !v)}>
              Menu
            </Button>
            <span className="hidden text-sm text-gray-500 sm:inline dark:text-gray-400">
              Enterprise RCM workspace
            </span>
          </div>
          <div className="flex items-center gap-3">
            <Button variant="secondary" type="button" onClick={() => setDark((d) => !d)}>
              {dark ? 'Light' : 'Dark'}
            </Button>
            <div className="text-right">
              <div className="text-sm font-semibold text-gray-900 dark:text-white">
                {user?.display_name}
              </div>
              <div className="text-xs text-gray-500">{user?.roles.join(', ')}</div>
            </div>
            <Button
              variant="ghost"
              type="button"
              onClick={() => {
                logout()
                navigate('/login')
              }}
            >
              Logout
            </Button>
          </div>
        </header>
        <main className="flex-1 overflow-auto p-4 md:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
