$ErrorActionPreference = "Continue"

$TaskName = "TaiwanStockDailyRun"

Write-Host "Removing Windows scheduled task..." -ForegroundColor Cyan
schtasks /Delete /TN $TaskName /F | Write-Host
Write-Host ""
Write-Host "Done." -ForegroundColor Green
Read-Host "Press Enter to close"
