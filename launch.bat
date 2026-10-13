@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Set up the app first. See README.md for installation instructions.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" desktop.py
if errorlevel 1 pause
