@echo off
REM ============================================================
REM  SatOps AI - stop whatever is holding port 8000
REM  Use this when run.bat reports "Port 8000 is already in use".
REM ============================================================
setlocal enabledelayedexpansion
set FOUND=0

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do (
  echo Stopping process %%a on port 8000...
  taskkill /PID %%a /F >nul 2>&1
  set FOUND=1
)

if "!FOUND!"=="0" (
  echo Port 8000 is already free - nothing to stop.
) else (
  echo Done. You can run run.bat now.
)

echo.
pause
