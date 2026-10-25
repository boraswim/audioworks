@echo off
rem Starts the Vocalworks desktop app (no console window).
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m app
