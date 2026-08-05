import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { Card, Badge } from '../components/ui'

export function PlatformPage() {
  const [platform, setPlatform] = useState<Record<string, unknown> | null>(null)
  const [health, setHealth] = useState<Array<Record<string, unknown>>>([])
  const [runs, setRuns] = useState<Array<Record<string, unknown>>>([])
  const [error, setError] = useState('')

  useEffect(() => {
    void (async () => {
      try {
        const [p, h, r] = await Promise.all([
          api<Record<string, unknown>>('/api/platform'),
          api<{ latest: Array<Record<string, unknown>> }>('/api/ops/health'),
          api<{ runs: Array<Record<string, unknown>> }>('/api/ops/runs?limit=10'),
        ])
        setPlatform(p)
        setHealth(h.latest || [])
        setRuns(r.runs || [])
      } catch (e) {
        setError(String((e as Error).message))
      }
    })()
  }, [])

  return (
    <div className="space-y-6">
      <div>
        <h1 className="font-display text-2xl font-semibold text-gray-900 dark:text-white">
          Platform Health
        </h1>
        <p className="mt-1 text-sm text-gray-500">Pipeline and monitoring overview.</p>
      </div>
      {error && <p className="text-sm text-rose-600">{error}</p>}
      <div className="grid gap-4 md:grid-cols-3">
        <Card title="Status">
          <div className="flex items-center gap-2">
            <Badge tone={platform?.status === 'healthy' ? 'green' : 'amber'}>
              {String(platform?.status || 'unknown')}
            </Badge>
            <span className="text-sm text-gray-500">v{String(platform?.version || '—')}</span>
          </div>
          <p className="mt-3 text-sm text-gray-600 dark:text-gray-300">
            Schema {String(platform?.schema_version || '—')} · git{' '}
            {String(platform?.git_sha || '—')}
          </p>
        </Card>
        <Card title="Last pipeline" className="md:col-span-2">
          <pre className="overflow-auto text-xs text-gray-600 dark:text-gray-300">
            {JSON.stringify(platform?.last_pipeline || null, null, 2)}
          </pre>
        </Card>
      </div>
      <Card title="Recent runs">
        <div className="overflow-x-auto">
          <table className="min-w-full text-left text-sm">
            <thead className="bg-gray-50 text-xs uppercase text-gray-500 dark:bg-gray-800">
              <tr>
                <th className="px-3 py-2">Run</th>
                <th className="px-3 py-2">Date</th>
                <th className="px-3 py-2">Status</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={String(r.run_id)} className="border-t border-gray-100 dark:border-gray-800">
                  <td className="px-3 py-2 font-mono text-xs">{String(r.run_id).slice(0, 8)}</td>
                  <td className="px-3 py-2">{String(r.as_of_date)}</td>
                  <td className="px-3 py-2">
                    <Badge tone={r.status === 'success' ? 'green' : 'amber'}>
                      {String(r.status)}
                    </Badge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card title="System health probes">
        <div className="grid gap-2 md:grid-cols-2">
          {health.map((h, i) => (
            <div
              key={i}
              className="rounded-lg border border-gray-100 px-3 py-2 text-sm dark:border-gray-800"
            >
              <div className="font-medium">
                {String(h.system_key)} / {String(h.probe_name)}
              </div>
              <div className="text-gray-500">{String(h.status)}</div>
            </div>
          ))}
          {!health.length && <p className="text-sm text-gray-500">No probes yet.</p>}
        </div>
      </Card>
    </div>
  )
}
