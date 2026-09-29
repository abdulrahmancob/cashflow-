import { useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { CompletedTodayBadge } from '../components/CompletedTodayBadge'
import { CollectionLookupsTab } from './CollectionLookups'
import { CollectionQueueTab } from './CollectionQueue'

type Tab = 'queue' | 'lookups'

function panelClass(active: boolean) {
  return active ? 'flex min-h-0 flex-1 flex-col' : 'hidden'
}

export function CollectionPage() {
  const { hasRole } = useAuth()
  const canLookups = hasRole('ops_admin', 'super_admin', 'sub_admin')
  const [tab, setTab] = useState<Tab>('queue')
  const [seen, setSeen] = useState<ReadonlySet<Tab>>(() => new Set<Tab>(['queue']))

  function openTab(next: Tab) {
    setTab(next)
    setSeen((cur) => {
      if (cur.has(next)) return cur
      const copy = new Set(cur)
      copy.add(next)
      return copy
    })
  }

  const tabs: Array<[Tab, string]> = canLookups
    ? [
        ['queue', 'Queue'],
        ['lookups', 'Lookups'],
      ]
    : [['queue', 'Queue']]

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2">
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-3">
          <div className="min-w-0">
            <h1 className="font-display text-xl font-semibold tracking-tight text-gray-900 dark:text-white">
              Collection
            </h1>
            <p className="text-xs text-gray-500">
              Denied visits and pending visits past insurance SLA
            </p>
          </div>
          <div className="flex w-fit shrink-0 gap-1 rounded-lg bg-gray-100 p-1 dark:bg-gray-800">
            {tabs.map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => openTab(key)}
                className={`rounded-md px-3 py-1 text-sm font-medium transition ${
                  tab === key
                    ? 'bg-white text-gray-900 shadow-xs dark:bg-gray-900 dark:text-white'
                    : 'text-gray-600 hover:text-gray-900 dark:text-gray-300 dark:hover:text-white'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
        <CompletedTodayBadge area="collection" compact />
      </div>
      {seen.has('queue') ? (
        <div className={panelClass(tab === 'queue')}>
          <CollectionQueueTab />
        </div>
      ) : null}
      {canLookups && seen.has('lookups') ? (
        <div className={panelClass(tab === 'lookups')}>
          <CollectionLookupsTab />
        </div>
      ) : null}
    </div>
  )
}
