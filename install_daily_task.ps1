$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = "TaiwanStockDailyRun"
$ScriptPath = Join-Path $AppDir "run_daily_task.ps1"
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
Write-Host "Schedule: Monday-Friday 15:30"
Write-Host "Action: $TaskAction"
Write-Host ""

schtasks /Create /TN $TaskName /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 15:30 /TR $TaskAction /F | Write-Host

Write-Host ""
Write-Host "Done. The task will run Monday-Friday at 15:30." -ForegroundColor Green
Write-Host "Logs will be saved under:"
Write-Host (Join-Path $AppDir "logs")
Write-Host ""
Write-Host "You can test it now from Task Scheduler, or run:"
Write-Host "schtasks /Run /TN $TaskName"
Write-Host ""
Read-Host "Press Enter to close"
