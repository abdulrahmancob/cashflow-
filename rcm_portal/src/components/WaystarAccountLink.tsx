import type { MouseEvent } from 'react'
import { waystarClaimsListingUrl } from './waystarClaimsListing'

export function WaystarAccountLink({
  accountNumber,
  patientName,
}: {
  accountNumber?: string | null
  patientName?: string | null
}) {
  const value = (accountNumber || '').trim()
  const name = (patientName || '').trim()
  if (!value) return <>{'—'}</>
  if (!name) return <>{value}</>
  return (
    <a
      href={waystarClaimsListingUrl(name)}
      target="_blank"
      rel="noopener noreferrer"
      className="text-brand-600 hover:underline dark:text-brand-300"
      onClick={(e: MouseEvent) => {
        e.stopPropagation()
      }}
    >
      {value}
    </a>
  )
}
