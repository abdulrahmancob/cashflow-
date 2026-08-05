# RCM Operations Portal

TailAdmin-style enterprise portal (React + Vite + Tailwind) for:

- **Posting Team** — Eligibility Work Queue
- **Finance** — Cashflow / forecast dashboards (read-only)
- **Super Admin** — Users, platform health, generate work items

## Dev

```bash
# API (repo root)
python -m cashflow_db migrate
python -m cashflow_db generate-eligibility --from-csv   # or from DB after recon
python -m cashflow_forecast.api                        # :8787

# Portal
cd rcm_portal
npm install
npm run dev                                            # :5174
```

Seeded portal users (create-if-missing on migrate/API startup; login with email):

| Email | Role | Default password env |
|-------|------|----------------------|
| `abdelrahman.hamdy@cobsolution.com` | super_admin | `CASHFLOW_SEED_ADMIN_PASSWORD` |
| `mostafa.ezz@cobsolution.com` | finance | `CASHFLOW_SEED_FINANCE_PASSWORD` |
| `billing7@cobsolution.com` (Ahmed Daker) | posting_team | `CASHFLOW_SEED_POSTING_PASSWORD` |

Set `CASHFLOW_JWT_SECRET` in production.

## Design

Unified TailAdmin analytics layout: sidebar, header, KPI cards, data grid, drawer, light/dark theme. Do not introduce alternate dashboard skins.
