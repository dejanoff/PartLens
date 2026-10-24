@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" partlens.py
) else (
  python partlens.py
)
if errorlevel 1 pause
