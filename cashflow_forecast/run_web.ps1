# Launch FastAPI (:8787) + Vite React (:5173) for the orbital forecast dashboard.
# Run from repo root OR from cashflow_forecast/.

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = Split-Path -Parent $Here

Set-Location $Repo

Write-Host "Installing Python API deps (fastapi/uvicorn) if needed…" -ForegroundColor Cyan
python -m pip install -q fastapi uvicorn pandas

Write-Host "Starting API on http://127.0.0.1:8787 …" -ForegroundColor Cyan
$api = Start-Process -PassThru -NoNewWindow python -ArgumentList "-m", "cashflow_forecast.api"

Set-Location (Join-Path $Here "web")
if (-not (Test-Path "node_modules")) {
  Write-Host "npm install…" -ForegroundColor Cyan
  npm install
}

Write-Host "Starting Vite on http://127.0.0.1:5173 …" -ForegroundColor Cyan
Write-Host "Open the Vite URL. Stop with Ctrl+C (then stop API PID $($api.Id) if needed)." -ForegroundColor Yellow
try {
  npm run dev
} finally {
  if ($api -and -not $api.HasExited) {
    Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue
  }
}
