@echo off
rem First-time setup (and repair): installs Python and the app's libraries into this folder,
rem then adds Desktop and Start menu shortcuts. See setup\install.ps1.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\install.ps1"
if errorlevel 1 (
  echo.
  echo Setup did not finish. Read the message above, fix it, and run this file again.
)
pause
