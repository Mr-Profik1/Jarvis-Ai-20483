@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0first-run.ps1"
if errorlevel 1 (
  echo.
  echo Something went wrong, see the message above.
  pause
)
