const WAYSTAR_CLAIMS = 'https://claims.zirmed.com/Claims/Listing/Index'

/** Option value posted by the claims search. The listing chip shows this as Last Year. */
const TRANSACTION_DATE_LAST_YEAR = 'Last 1 Year'

export function waystarClaimsListingUrl(patientName: string) {
  const name = patientName.trim()
  const params = new URLSearchParams({
    appid: '1',
    explicitSearch: 'True',
    'SearchCriteria.Status': '-1',
    'SearchCriteria.TransactionDateSpan': TRANSACTION_DATE_LAST_YEAR,
    'SearchCriteria.PatientNames': name,
    'SearchCriteria.ServiceDateSpan': 'All',
    'SearchCriteria.Archived': '0',
  })
  return `${WAYSTAR_CLAIMS}?${params.toString()}`
}
