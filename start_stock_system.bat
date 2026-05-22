@echo off
set "APP_DIR=%~dp0"
set "PS_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if not exist "%PS_EXE%" (
  echo PowerShell was not found.
  echo Please open this folder in Codex and ask for help.
  pause
  exit /b 1
)

"%PS_EXE%" -NoExit -ExecutionPolicy Bypass -File "%APP_DIR%start_stock_system.ps1"
