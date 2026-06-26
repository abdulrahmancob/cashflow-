# Run Phase 1 discovery in parallel (one process per facility).
# Usage: .\scripts\run_discovery_pools.ps1 [-Days 10] [-LookaheadDays 10] [-OutputRoot output]

param(
    [int]$Days = 10,
    [int]$LookaheadDays = 10,
    [string]$OutputRoot = "output",
    [string[]]$FacilityIds = @()
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

if ($FacilityIds.Count -eq 0) {
    Write-Host "Fetching facility IDs from WebPT..."
    $listScript = @'
import asyncio
from playwright.async_api import async_playwright
from auth import create_context, ensure_authenticated, list_clinics
from config import WebPTConfig

async def main():
    config = WebPTConfig.from_env()
    async with async_playwright() as p:
        ctx = await create_context(p, config)
        page = await ctx.new_page()
        await ensure_authenticated(page, ctx, config)
        clinics = await list_clinics(page, config.company_id)
        await ctx.browser.close()
    for c in clinics:
        print(c.facility_id)

asyncio.run(main())
'@
    $FacilityIds = & $Python -c $listScript
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to list clinics. Run: python scraper.py login"
    }
}

Write-Host "Starting discovery for $($FacilityIds.Count) facilities..."
$jobs = @()

foreach ($fid in $FacilityIds) {
    $fid = $fid.Trim()
    if (-not $fid) { continue }
    $outDir = Join-Path $OutputRoot "discover_$fid"
    $logFile = Join-Path $outDir "discovery.log"
    New-Item -ItemType Directory -Force -Path $outDir | Out-Null

    $argList = @(
        "scraper.py", "--headless", "export-recent-appointments",
        "--days", $Days,
        "--lookahead-days", $LookaheadDays,
        "--skip-edocs", "--skip-chart-notes", "--skip-ocr",
        "--facility-id", $fid,
        "--output", $outDir
    )

    Write-Host "  Facility $fid -> $outDir"
    $jobs += Start-Process -FilePath $Python -ArgumentList $argList `
        -WorkingDirectory $Root -RedirectStandardOutput $logFile `
        -RedirectStandardError $logFile -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 500
}

Write-Host "Waiting for $($jobs.Count) discovery jobs..."
$jobs | ForEach-Object { $_.WaitForExit() }

$failed = @($jobs | Where-Object { $_.ExitCode -ne 0 })
if ($failed.Count -gt 0) {
    Write-Warning "$($failed.Count) discovery job(s) failed. Check logs under $OutputRoot/discover_*/discovery.log"
}

$discoverDirs = Get-ChildItem -Path $OutputRoot -Directory -Filter "discover_*" |
    ForEach-Object { $_.FullName }

if ($discoverDirs.Count -eq 0) {
    throw "No discover_* folders found under $OutputRoot"
}

Write-Host "Merging CSVs into $OutputRoot/recent_10d_merged ..."
& $Python merge_export_csv.py --input @discoverDirs --output (Join-Path $OutputRoot "recent_10d_merged")
if ($LASTEXITCODE -ne 0) { throw "merge_export_csv.py failed" }

Write-Host "Done. Next: python scraper.py parallel-download --input $OutputRoot/recent_10d_merged/patients_recent_10d.csv --output $OutputRoot/recent_10d_merged --workers 8"
