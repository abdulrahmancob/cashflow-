# Waystar Denials Exploration Findings

Generated: 2026-07-09T12:20:59.765927+00:00

## Navigation

1. WorkCenter entry: `https://claims.zirmed.com/WorkCenter/WorkCenterMessage.aspx?TabAppID=40&Error=1`
2. Denials Workcenter: `https://denials.zirmed.com/Workcenter?appid=41`
3. Review sample: `https://denials.zirmed.com/Review?denialID=3652667586&appId=41&tabAppId=40&returnToWorkCenter=True`

## Workcenter List

- Final URL: `https://denials.zirmed.com/Workcenter?appid=41`
- HTML size: 69,673 bytes
- Review links found: 0
- data-denialid attributes: 3
- Sample denial IDs: 3793709968
- List appears server-rendered HTML: **yes**

## Review Detail

- Denial ID: `3652667586`
- Final URL: `https://denials.zirmed.com/Review?denialID=3652667586&appId=41&tabAppId=40&returnToWorkCenter=True`
- Has #remitsContainer: True
- historyRemitLink count: 4
- remitTable count: 4
- CARC codes sample: CO-45, CO-97, PI-94, PR-242, PI-151
- Adjustment statuses: Closed, Active, Inactive

## Parser Notes

- Workcenter: parse denial rows from HTML grid; denial_id from Review links or data-denialid.
- Review: parse `a.historyRemitLink` + `table.remitTable` rows including hidden inactive remits.
- Tooltips: CARC/RARC descriptions are embedded in `.toolTipInner` (no hover needed).
- Resolution: Closed adjustments include WriteOff/Paid + date + Automatic Rule or Manual ID.
