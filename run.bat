@echo off
REM ============================================================
REM  SatOps AI - start the mission ops dashboard
REM  Double-click this file, or run  run.bat  from a terminal.
REM ============================================================
cd /d "%~dp0"
title SatOps AI - Mission Ops Server

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Virtual environment not found at .venv\Scripts\python.exe
  echo         Create it with:  python -m venv .venv
  echo         Then install:    .venv\Scripts\pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo Starting SatOps AI...
echo Dashboard will be at http://localhost:8000/dashboard
echo Press Ctrl+C in this window to stop the server.
echo.

REM -u keeps agent logs unbuffered so errors appear live.
".venv\Scripts\python.exe" -u main.py

echo.
echo Server stopped.
pause
