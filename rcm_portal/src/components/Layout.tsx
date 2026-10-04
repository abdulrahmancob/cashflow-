import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useEffect, useState } from 'react'
import {
  Activity,
  BarChart3,
  ClipboardList,
  Database,
  FileSpreadsheet,
  LineChart,
  ListChecks,
  LogOut,
  Menu,
  Moon,
  PanelLeftClose,
  Sun,
  Timer,
  Users,
  Wallet,
  X,
} from 'lucide-react'
import { AwayBanner, AwayControl, AwayProvider } from './AwayControl'
import { DeskPresenceButton } from './DeskPresenceButton'
import { ChatWidget } from './ChatWidget'
import { useAuth } from '../auth/AuthContext'
import { useActivityHeartbeat } from '../auth/useActivityHeartbeat'
import type { Role } from '../api/client'
import { RemitarcMark } from './RemitarcMark'
import { Avatar } from './table'
import { Button } from './ui'

type NavItem = {
  to: string
  label: string
  icon: typeof ClipboardList
  roles?: Role[]
  trackerView?: boolean
  checksView?: boolean
}

const GROUPS: Array<{ label: string; items: NavItem[] }> = [
  {
    label: 'Operations',
    items: [
      {
        to: '/eligibility',
        label: 'Eligibility Sheet',
        icon: ClipboardList,
        roles: ['posting_team', 'super_admin', 'sub_admin', 'collector', 'finance', 'ops_admin'],
      },
      {
        to: '/analytics',
        label: 'Team Analytics',
        icon: BarChart3,
        roles: ['super_admin', 'sub_admin', 'ops_admin', 'second_submission_lead', 'analytics_viewer'],
      },
      {
        to: '/second-submission',
        label: 'Second Submission',
        icon: ClipboardList,
        roles: ['second_submission', 'second_submission_lead', 'super_admin', 'sub_admin', 'ops_admin'],
      },
      {
        to: '/patient-responsibility',
        label: 'Patient Responsibility',
        icon: ClipboardList,
        roles: [
          'posting_team',
          'second_submission',
          'second_submission_lead',
          'super_admin',
          'sub_admin',
          'ops_admin',
          'finance',
        ],
      },
      {
        to: '/collection',
        label: 'Collection',
        icon: ClipboardList,
        roles: ['posting_team', 'super_admin', 'sub_admin', 'collector', 'finance', 'ops_admin'],
      },
      {
        to: '/cpt-guide',
        label: 'CPT Guide',
        icon: FileSpreadsheet,
        roles: ['posting_team', 'super_admin', 'sub_admin', 'finance', 'submission', 'ops_admin'],
      },
      {
        to: '/cpt-audit',
        label: 'CPT / ICD / Demo',
        icon: ListChecks,
        roles: ['medical_audit', 'super_admin', 'sub_admin', 'ops_admin'],
      },
      { to: '/tracker', label: 'Transaction Tracker', icon: Wallet, trackerView: true },
      { to: '/checks-deposits', label: 'Checks & Deposits', icon: Wallet, checksView: true },
      {
        to: '/away',
        label: 'Away board',
        icon: Timer,
        roles: ['super_admin', 'sub_admin', 'ops_admin', 'second_submission_lead'],
      },
    ],
  },
  {
    label: 'Finance',
    items: [
      {
        to: '/finance/cash',
        label: 'Cash Trajectory',
        icon: LineChart,
        roles: ['finance', 'super_admin', 'sub_admin'],
      },
      {
        to: '/finance/mission',
        label: 'Mission Control',
        icon: BarChart3,
        roles: ['finance', 'super_admin', 'sub_admin'],
      },
      {
        to: '/finance/insights',
        label: 'Business Insights',
        icon: BarChart3,
        roles: ['finance', 'super_admin', 'sub_admin'],
      },
      {
        to: '/finance/unbanked',
        label: 'Checks vs tracker',
        icon: Wallet,
        roles: ['finance', 'super_admin', 'sub_admin'],
      },
      {
        to: '/finance/overdue',
        label: 'Overdue',
        icon: Activity,
        roles: ['finance', 'super_admin', 'sub_admin'],
      },
      {
        to: '/billing-analysis',
        label: 'Billing cash flow',
        icon: LineChart,
        roles: ['super_admin', 'sub_admin', 'ops_admin'],
      },
    ],
  },
  {
    label: 'Admin',
    items: [
      { to: '/activity', label: 'Activity', icon: Activity, roles: ['super_admin', 'sub_admin', 'ops_admin'] },
      { to: '/platform', label: 'Platform Health', icon: Activity, roles: ['super_admin'] },
      { to: '/database', label: 'Database', icon: Database, roles: ['super_admin'] },
      { to: '/users', label: 'Users', icon: Users, roles: ['super_admin', 'sub_admin'] },
    ],
  },
]

const PAGE_TITLES: Array<[string, string]> = [
  ['/eligibility', 'Eligibility Sheet'],
  ['/patient-responsibility', 'Patient Responsibility'],
  ['/collection', 'Collection'],
  ['/analytics', 'Team Analytics'],
  ['/cpt-guide', 'CPT Guide'],
  ['/cpt-audit', 'CPT / ICD / Demographics'],
  ['/tracker', 'Transaction Tracker'],
  ['/checks-deposits', 'Checks & Deposits'],
  ['/finance/cash', 'Cash Trajectory'],
  ['/finance/mission', 'Mission Control'],
  ['/finance/insights', 'Business Insights'],
  ['/finance/unbanked', 'Checks vs tracker'],
  ['/finance/overdue', 'Overdue'],
  ['/finance/drill', 'Overdue'],
  ['/billing-analysis', 'Billing cash flow'],
  ['/second-submission', 'Second Submission'],
  ['/away', 'Away board'],
  ['/activity', 'Activity'],
  ['/platform', 'Platform Health'],
  ['/database', 'Database'],
  ['/users', 'Users'],
]

function pageTitle(pathname: string) {
  const hit = PAGE_TITLES.find(([p]) => pathname.startsWith(p))
  return hit?.[1] || 'Remitarc'
}

type SidebarBodyProps = {
  compact?: boolean
  groups: Array<{ label: string; items: NavItem[] }>
  displayName: string
  roles: string
  onLogout: () => void
}

function SidebarBody({ compact, groups, displayName, roles, onLogout }: SidebarBodyProps) {
  return (
    <>
      <div
        className={`flex h-16 items-center gap-2.5 border-b border-gray-200 dark:border-gray-800 ${
          compact ? 'justify-center px-2' : 'px-4'
        }`}
      >
        <RemitarcMark />
        {!compact && (
          <div className="min-w-0">
            <div className="font-display truncate text-sm font-semibold text-gray-900 dark:text-white">
              Remitarc
            </div>
            <div className="whitespace-nowrap text-[11px] leading-4 tracking-tight text-gray-500">
              The remit, before it posts.
            </div>
          </div>
        )}
      </div>
      <nav className="flex-1 space-y-5 overflow-y-auto p-3">
        {groups.map((group) => (
          <div key={group.label}>
            {!compact && (
              <div className="mb-1.5 px-2 text-[11px] font-semibold uppercase tracking-wider text-gray-400">
                {group.label}
              </div>
            )}
            <div className="space-y-0.5">
              {group.items.map((item) => {
                const Icon = item.icon
                return (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    title={compact ? item.label : undefined}
                    className={({ isActive }) =>
                      `flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm font-medium transition ${
                        isActive
                          ? 'bg-brand-50 text-brand-700 dark:bg-brand-700/20 dark:text-brand-100'
                          : 'text-gray-600 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800'
                      } ${compact ? 'justify-center' : ''}`
                    }
                  >
                    <Icon className="h-4 w-4 shrink-0" />
                    {!compact && <span className="truncate">{item.label}</span>}
                  </NavLink>
                )
              })}
            </div>
          </div>
        ))}
      </nav>
      <div className="border-t border-gray-200 p-3 dark:border-gray-800">
        <div className={`flex items-center gap-2.5 rounded-lg p-2 ${compact ? 'justify-center' : ''}`}>
          <Avatar name={displayName || 'User'} size="sm" />
          {!compact && (
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-semibold text-gray-900 dark:text-white">
                {displayName}
              </div>
              <div className="truncate text-xs text-gray-500">{roles}</div>
            </div>
          )}
          {!compact && (
            <button
              type="button"
              title="Logout"
              className="rounded-md p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-800"
              onClick={onLogout}
            >
              <LogOut className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>
    </>
  )
}

export function Layout() {
  const { user, logout, hasRole, canTracker, canChecks } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [dark, setDark] = useState(() => localStorage.getItem('rcm_theme') === 'dark')
  const [collapsed, setCollapsed] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)
  useActivityHeartbeat()

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    localStorage.setItem('rcm_theme', dark ? 'dark' : 'light')
  }, [dark])

  useEffect(() => {
    setMobileOpen(false)
  }, [location.pathname])

  const visible = GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((n) => {
      if (n.trackerView) return canTracker('view') || hasRole('ops_admin', 'sub_admin')
      if (n.checksView) return canChecks('view') || hasRole('ops_admin', 'sub_admin')
      return n.roles ? hasRole(...n.roles) : false
    }),
  })).filter((g) => g.items.length)

  const sidebarProps = {
    groups: visible,
    displayName: user?.display_name || 'User',
    roles: user?.roles.join(', ') || '',
    onLogout: () => {
      void logout().finally(() => navigate('/login'))
    },
  }

  return (
    <AwayProvider>
    <div className="flex h-dvh overflow-hidden bg-gray-50 dark:bg-gray-950">
      <aside
        className={`hidden min-h-0 shrink-0 flex-col border-r border-gray-200 bg-white transition-all dark:border-gray-800 dark:bg-gray-900 lg:flex ${
          collapsed ? 'w-[72px]' : 'w-64'
        }`}
      >
        <SidebarBody compact={collapsed} {...sidebarProps} />
      </aside>

      {mobileOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <button
            className="absolute inset-0 bg-gray-900/40 backdrop-blur-[2px]"
            aria-label="Close menu"
            onClick={() => setMobileOpen(false)}
          />
          <aside className="relative flex h-full w-64 flex-col bg-white shadow-2xl dark:bg-gray-900">
            <button
              type="button"
              className="absolute right-3 top-4 rounded-md p-1 text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800"
              onClick={() => setMobileOpen(false)}
              aria-label="Close"
            >
              <X className="h-4 w-4" />
            </button>
            <SidebarBody {...sidebarProps} />
          </aside>
        </div>
      )}

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <header className="relative z-20 flex h-16 shrink-0 items-center justify-between border-b border-gray-200 bg-white px-4 dark:border-gray-800 dark:bg-gray-900">
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="icon"
              type="button"
              className="lg:hidden"
              onClick={() => setMobileOpen(true)}
              aria-label="Open menu"
            >
              <Menu className="h-5 w-5" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              type="button"
              className="hidden lg:inline-flex"
              onClick={() => setCollapsed((v) => !v)}
              aria-label="Toggle sidebar"
            >
              <PanelLeftClose className="h-5 w-5" />
            </Button>
            <span className="font-display text-sm font-semibold text-gray-900 dark:text-white">
              {pageTitle(location.pathname)}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <DeskPresenceButton />
            <AwayControl />
            <Button
              variant="ghost"
              size="icon"
              type="button"
              onClick={() => setDark((d) => !d)}
              aria-label={dark ? 'Switch to light mode' : 'Switch to dark mode'}
            >
              {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </Button>
            <div className="hidden text-right sm:block">
              <div className="text-sm font-semibold text-gray-900 dark:text-white">
                {user?.display_name}
              </div>
              <div className="text-xs text-gray-500">{user?.roles.join(', ')}</div>
            </div>
          </div>
        </header>
        <AwayBanner />
        <main
          className={
            location.pathname.startsWith('/eligibility') ||
              location.pathname.startsWith('/collection') ||
              location.pathname.startsWith('/patient-responsibility')
              ? 'flex min-h-0 flex-1 flex-col overflow-hidden p-3 md:p-4'
              : 'flex-1 overflow-auto p-4 md:p-6'
          }
        >
          <Outlet />
        </main>
      </div>
      {hasRole('super_admin', 'sub_admin') && <ChatWidget page={location.pathname} />}
    </div>
    </AwayProvider>
  )
}
