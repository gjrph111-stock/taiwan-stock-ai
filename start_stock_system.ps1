$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $AppDir

$BundledPython = "C:\Users\user\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$PythonExe = $BundledPython

if (-not (Test-Path $PythonExe)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        $PythonExe = $cmd.Source
    }
}

function Pause-Menu {
    Write-Host ""
    Read-Host "Press Enter to continue"
}

function Run-StockCommand {
    param([string[]]$ArgsList)

    if (-not (Test-Path $PythonExe)) {
        Write-Host ""
        Write-Host "Python executable was not found." -ForegroundColor Red
        Write-Host "Expected path:"
        Write-Host $BundledPython
        Write-Host ""
        Write-Host "Tell Codex: Python was not found."
        Pause-Menu
        return
    }

    Write-Host ""
    Write-Host "Running: $PythonExe $($ArgsList -join ' ')" -ForegroundColor Cyan
    & $PythonExe @ArgsList
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "Command failed. Please copy the error above to Codex." -ForegroundColor Red
    }
    Pause-Menu
}

while ($true) {
    Clear-Host
    Write-Host "=========================================="
    Write-Host "Taiwan Stock Data System V1"
    Write-Host "=========================================="
    Write-Host ""
    Write-Host "Folder:"
    Write-Host $AppDir
    Write-Host ""
    Write-Host "Python:"
    Write-Host $PythonExe
    Write-Host ""
    Write-Host "1. Show database status"
    Write-Host "2. Update TWSE/TPEX stock list"
    Write-Host "3. Update sample stocks: 2330 / 2317 / 6488"
    Write-Host "4. Update all stocks, last 3 years"
    Write-Host "5. Show one stock summary"
    Write-Host "6. Show one stock indicators"
    Write-Host "7. Market scan report"
    Write-Host "8. Signal ranking"
    Write-Host "9. Daily Top 5 watchlist"
    Write-Host "10. Backtest signal model"
    Write-Host "11. Optimize signal backtest"
    Write-Host "12. Compare risk filter"
    Write-Host "13. Feature contribution analysis"
    Write-Host "14. Strategy portfolio backtest"
    Write-Host "15. Realistic strategy backtest"
    Write-Host "16. Start web dashboard"
    Write-Host "17. Preview push notification"
    Write-Host "18. Send Telegram report"
    Write-Host "19. Send LINE report"
    Write-Host "20. Daily run: update data and send Telegram"
    Write-Host "21. Show recent runs"
    Write-Host "22. Exit"
    Write-Host ""

    $choice = Read-Host "Choose 1-22"

    switch ($choice) {
        "1" { Run-StockCommand @("-m", "stock_v1", "status") }
        "2" { Run-StockCommand @("-m", "stock_v1", "universe") }
        "3" { Run-StockCommand @("-m", "stock_v1", "update", "--codes", "2330,2317,6488", "--years", "1", "--pause", "0") }
        "4" {
            $confirm = Read-Host "This may take a while. Type Y to start"
            if ($confirm -eq "Y" -or $confirm -eq "y") {
                Run-StockCommand @("-m", "stock_v1", "update", "--years", "3")
            }
        }
        "5" {
            $code = Read-Host "Stock code, for example 2330"
            if ($code) {
                Run-StockCommand @("-m", "stock_v1", "stock", $code)
            }
        }
        "6" {
            $code = Read-Host "Stock code, for example 2330"
            if ($code) {
                Run-StockCommand @("-m", "stock_v1", "indicators", $code)
            }
        }
        "7" { Run-StockCommand @("-m", "stock_v1", "scan", "--limit", "20") }
        "8" { Run-StockCommand @("-m", "stock_v1", "signals", "--limit", "20") }
        "9" { Run-StockCommand @("-m", "stock_v1", "watchlist", "--limit", "5") }
        "10" { Run-StockCommand @("-m", "stock_v1", "backtest", "--top", "10", "--horizon", "5", "--step", "5", "--max-days", "260") }
        "11" { Run-StockCommand @("-m", "stock_v1", "optimize", "--tops", "5,10,20", "--horizons", "5,10,20", "--step", "5", "--max-days", "260") }
        "12" { Run-StockCommand @("-m", "stock_v1", "risk-filter", "--top", "5", "--horizons", "5,10,20", "--step", "5", "--max-days", "260") }
        "13" { Run-StockCommand @("-m", "stock_v1", "features", "--horizon", "10", "--step", "5", "--max-days", "260") }
        "14" { Run-StockCommand @("-m", "stock_v1", "strategy", "--top", "5", "--horizon", "20", "--step", "5", "--max-days", "260") }
        "15" { Run-StockCommand @("-m", "stock_v1", "realistic-strategy", "--positions", "5", "--horizon", "20", "--step", "5", "--max-days", "260", "--cost-bps", "20", "--export") }
        "16" { Run-StockCommand @("-m", "stock_v1", "web", "--port", "8765") }
        "17" { Run-StockCommand @("-m", "stock_v1", "notify-preview", "--limit", "5") }
        "18" { Run-StockCommand @("-m", "stock_v1", "notify-telegram", "--limit", "5") }
        "19" { Run-StockCommand @("-m", "stock_v1", "notify-line", "--limit", "5") }
        "20" {
            $confirm = Read-Host "This updates all stocks and sends Telegram. Type Y to start"
            if ($confirm -eq "Y" -or $confirm -eq "y") {
                Run-StockCommand @("-m", "stock_v1", "daily-run", "--limit", "5")
            }
        }
        "21" { Run-StockCommand @("-m", "stock_v1", "runs", "--limit", "10") }
        "22" { break }
    }
}
