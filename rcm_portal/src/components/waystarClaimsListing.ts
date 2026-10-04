const WAYSTAR_SEARCH = 'https://claims.zirmed.com/Claims/Listing/Search'

/**
 * Index only draws the empty form, and PerformSearch rejects GET.
 * The listing script loads results from Search, and its controls are:
 * Status "All" (the empty page defaults to "All Rejected"),
 * TransDate "1 year" (the Last Year option; "90 days" is the empty-page default).
 */
const STATUS_ALL = 'All'
const TRANSACTION_DATE_LAST_YEAR = '1 year'

export function waystarClaimsListingUrl(patientName: string) {
  const name = patientName.trim()
  const params = new URLSearchParams({
    appid: '1',
    'SearchCriteria.Status': STATUS_ALL,
    'SearchCriteria.TransDate': TRANSACTION_DATE_LAST_YEAR,
    'SearchCriteria.PatientNames': name,
    'SearchCriteria.ServiceDate': 'All',
    'SearchCriteria.Archived': '0',
  })
  return `${WAYSTAR_SEARCH}?${params.toString()}`
}
