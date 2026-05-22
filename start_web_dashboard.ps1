$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $AppDir

$PythonExe = "C:\Users\user\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (-not (Test-Path $PythonExe)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        $PythonExe = $cmd.Source
    }
}

if (-not (Test-Path $PythonExe)) {
    Write-Host "Python executable was not found." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host "Taiwan Stock Dashboard" -ForegroundColor Cyan
Write-Host ""
Write-Host "Keep this window open while using the web page."
Write-Host "Open this URL in your browser:"
Write-Host ""
Write-Host "http://127.0.0.1:8765" -ForegroundColor Green
Write-Host ""
Write-Host "Press Ctrl+C in this window to stop the dashboard."
Write-Host ""

& $PythonExe -m stock_v1 web --port 8765
