$ErrorActionPreference = "Continue"

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $AppDir

$LogDir = Join-Path $AppDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$LogPath = Join-Path $LogDir "intraday_run_$Stamp.log"
$PythonExe = "C:\Users\user\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if (-not (Test-Path $PythonExe)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        $PythonExe = $cmd.Source
    }
}

function Write-Log {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
    Write-Host $line
}

Write-Log "Taiwan stock intraday AI monitor task started."
Write-Log "AppDir=$AppDir"
Write-Log "Python=$PythonExe"

if (-not (Test-Path $PythonExe)) {
    Write-Log "Python executable was not found."
    exit 1
}

& $PythonExe -m stock_v1 notify-intraday --limit 5 *>> $LogPath
$ExitCode = $LASTEXITCODE

Write-Log "Intraday task finished with exit code $ExitCode."
exit $ExitCode
