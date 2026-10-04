import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { waystarClaimsListingUrl } from '../src/components/waystarClaimsListing.ts'

const url = waystarClaimsListingUrl('  BAZILE, JEAN  ')
const parsed = new URL(url)

assert.equal(parsed.origin + parsed.pathname, 'https://claims.zirmed.com/Claims/Listing/Index')
assert.equal(parsed.searchParams.get('appid'), '1')
assert.equal(parsed.searchParams.get('explicitSearch'), 'True')
assert.equal(parsed.searchParams.get('SearchCriteria.Status'), '-1')
assert.equal(parsed.searchParams.get('SearchCriteria.TransactionDateSpan'), 'Last 1 Year')
assert.equal(parsed.searchParams.get('SearchCriteria.PatientNames'), 'BAZILE, JEAN')
assert.equal(parsed.searchParams.get('SearchCriteria.ServiceDateSpan'), 'All')
assert.equal(parsed.searchParams.get('SearchCriteria.Archived'), '0')

const link = readFileSync(new URL('../src/components/WaystarAccountLink.tsx', import.meta.url), 'utf8')
assert.equal(link.includes('waystarClaimsListingUrl'), true)
assert.equal(link.includes('target="_blank"'), true)
assert.equal(link.includes('stopPropagation'), true)
assert.equal(link.includes('preventDefault'), false)

console.log('waystarClaimsListing checks ok')
