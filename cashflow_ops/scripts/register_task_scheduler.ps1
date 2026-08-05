# Register Windows Task Scheduler trigger for the RCM Processing Platform.
# The task ONLY triggers the workflow engine — it is not the orchestrator.
#
# Usage (Admin PowerShell):
#   .\cashflow_ops\scripts\register_task_scheduler.ps1
#   .\cashflow_ops\scripts\register_task_scheduler.ps1 -Python "D:\cashflow\code\.venv\Scripts\python.exe"

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$Python = "",
    [string]$TaskName = "CashflowRCMDailyPipeline",
    [string]$Time = "02:00"
)

if (-not $Python) {
    $venvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { $Python = $venvPy } else { $Python = "python" }
}

$arg = "-m cashflow_ops run --trigger task_scheduler --as-of `"$((Get-Date).ToString('yyyy-MM-dd'))`""
# Note: Task Scheduler should set StartIn = RepoRoot so imports resolve.
$action = New-ScheduledTaskAction -Execute $Python -Argument "-m cashflow_ops run --trigger task_scheduler" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Registered task '$TaskName' daily at $Time (Africa/Cairo wall clock depends on OS TZ)."
Write-Host "WorkingDirectory: $RepoRoot"
Write-Host "Python: $Python"
Write-Host "Engine owns resume/state — re-run: $Python -m cashflow_ops resume --run-id <id>"
