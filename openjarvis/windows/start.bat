@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0first-run.ps1"
if errorlevel 1 (
  echo.
  echo Что-то пошло не так, см. сообщение выше.
  pause
)
