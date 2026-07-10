param(
    [Parameter(Position = 0)]
    [ValidateSet("build", "sla", "dashboard", "test")]
    [string]$Command = "build"
)

# Run from cashflow_forecast/ with local venv — sets repo root on PYTHONPATH
$RepoRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = $RepoRoot
$Python = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}
Set-Location $RepoRoot

switch ($Command) {
    "build" {
        & $Python -m cashflow_forecast build `
            --data-dir webpt_edco_scraper/output/jun_jul_2026 `
            --output-dir webpt_edco_scraper/output/jun_jul_2026/forecast `
            --as-of 2026-07-09
    }
    "sla" {
        & $Python -m cashflow_forecast sla `
            --reconciliation-dir webpt_edco_scraper/output/jun_jul_2026/reconciliation `
            --output webpt_edco_scraper/output/jun_jul_2026/forecast/payer_sla.csv
    }
    "dashboard" {
        & $Python -m streamlit run cashflow_forecast/dashboard.py
    }
    "test" {
        & $Python -m pytest cashflow_forecast/tests -v
    }
}
