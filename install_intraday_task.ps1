$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = "TaiwanStockIntradayMonitor"
$ScriptPath = Join-Path $AppDir "run_intraday_task.ps1"
$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

if (-not (Test-Path $ScriptPath)) {
    Write-Host "Cannot find task runner:" -ForegroundColor Red
    Write-Host $ScriptPath
    Read-Host "Press Enter to close"
    exit 1
}

$TaskAction = "`"$PowerShell`" -NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""

Write-Host "Installing Windows scheduled task..." -ForegroundColor Cyan
Write-Host "Task name: $TaskName"
Write-Host "Schedule: daily 09:00-14:00, every 30 minutes; command skips non-weekdays."
Write-Host "Action: $TaskAction"
Write-Host ""

schtasks /Create /TN $TaskName /SC DAILY /ST 09:00 /RI 30 /DU 05:00 /TR $TaskAction /F | Write-Host

Write-Host ""
Write-Host "Done. The task will run during Taiwan market hours." -ForegroundColor Green
Write-Host "Logs will be saved under:"
Write-Host (Join-Path $AppDir "logs")
Write-Host ""
Write-Host "You can test during market hours with:"
Write-Host "schtasks /Run /TN $TaskName"
Write-Host ""
Read-Host "Press Enter to close"
