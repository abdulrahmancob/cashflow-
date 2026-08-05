# Cash Flow Forecast — Pilot

## Quick start

**Important:** run commands from the **repo root** (`D:\cashflow\code`).

### Build forecast data

```powershell
cd D:\cashflow\code
python -m cashflow_forecast build `
  --data-dir webpt_edco_scraper/output/jun_jul_2026 `
  --output-dir webpt_edco_scraper/output/jun_jul_2026/forecast `
  --as-of 2026-07-17
```

### React dashboard (primary)

```powershell
cd D:\cashflow\code
pip install -r cashflow_forecast/requirements.txt

# Terminal 1 — API
python -m cashflow_forecast.api

# Terminal 2 — UI
cd cashflow_forecast\web
npm install
npm run dev
```

Open **http://127.0.0.1:5173** (Vite proxies `/api` → `:8787`).

Or one helper: `.\cashflow_forecast\run_web.ps1`

Tabs: Mission Control · Cash Trajectory · Business Insights (audit) · Drill Decks.

### Streamlit fallback (optional)

```powershell
cd D:\cashflow\code
streamlit run cashflow_forecast/dashboard.py
```

(`dashboard.py` adds the repo root to `sys.path` so this works without `pip install -e .`.)

### Payer SLA only

```powershell
python -m cashflow_forecast sla `
  --reconciliation-dir webpt_edco_scraper/output/jun_jul_2026/reconciliation `
  --output webpt_edco_scraper/output/jun_jul_2026/forecast/payer_sla.csv
```

## Design rules

- **Outcome** (`paid|on_track|overdue|rejected|denied|zero_pay`) ≠ **Risk** (`audit_cpt|audit_icd|unsubmitted`)
- Loaders under `loaders/` each read one source only
- Audit↔Waystar linking uses numeric `match_score` (0–100), not High/Medium/Low
