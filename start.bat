@echo off
title PodHub Calling Dashboard Launcher
echo ===================================================
echo Starting PodHub Calling Dashboard (Phase 1)
echo ===================================================

set PODHUB_ALLOW_REAL_DIAL=1

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found! Please ensure virtualenv is set up.
    pause
    exit /b 1
)

echo [1/2] Launching Backend Server on http://127.0.0.1:8000...
start "PodHub Backend" cmd /k ".venv\Scripts\python.exe -m uvicorn backend:app --host 127.0.0.1 --port 8000"

timeout /t 3 /nobreak >nul

echo [2/2] Launching Ananya Voice Worker...
start "PodHub Worker" cmd /k ".venv\Scripts\python.exe main.py"

echo ===================================================
echo Backend and Worker launched in separate windows!
echo To place a test call, run:
echo   .venv\Scripts\python.exe test_call.py "+91XXXXXXXXXX"
echo ===================================================

