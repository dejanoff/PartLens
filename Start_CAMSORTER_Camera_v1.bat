@echo off
cd /d "%~dp0"
python CAMSORTER_Camera_v1.py
if errorlevel 1 pause
