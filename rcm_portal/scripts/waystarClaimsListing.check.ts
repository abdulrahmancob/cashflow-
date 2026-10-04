import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { waystarClaimsListingUrl } from '../src/components/waystarClaimsListing.ts'

const url = waystarClaimsListingUrl('  BAZILE, JEAN  ')
const parsed = new URL(url)

assert.equal(parsed.origin + parsed.pathname, 'https://claims.zirmed.com/Claims/Listing/Search')
assert.equal(parsed.searchParams.get('appid'), '1')
assert.equal(parsed.searchParams.get('SearchCriteria.Status'), 'All')
assert.equal(parsed.searchParams.get('SearchCriteria.TransDate'), '1 year')
assert.equal(parsed.searchParams.get('SearchCriteria.PatientNames'), 'BAZILE, JEAN')
assert.equal(parsed.searchParams.get('SearchCriteria.ServiceDate'), 'All')
assert.equal(parsed.searchParams.get('SearchCriteria.Archived'), '0')
assert.equal(parsed.searchParams.get('SearchCriteria.TransactionDateSpan'), null)
assert.equal(parsed.searchParams.get('explicitSearch'), null)
assert.equal(parsed.pathname.includes('/Index'), false)

const link = readFileSync(new URL('../src/components/WaystarAccountLink.tsx', import.meta.url), 'utf8')
assert.equal(link.includes('waystarClaimsListingUrl'), true)
assert.equal(link.includes('target="_blank"'), true)
assert.equal(link.includes('stopPropagation'), true)
assert.equal(link.includes('preventDefault'), false)

console.log('waystarClaimsListing checks ok')
