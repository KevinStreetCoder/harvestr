@echo off
REM ===========================================================================
REM  Stop a detached Harvestr started by start-harvestr.bat.
REM
REM  Needed because pythonw.exe has no console window - there is nothing to
REM  Ctrl+C, and Task Manager just shows an anonymous "pythonw.exe".
REM
REM    stop-harvestr.bat            -> stops the instance on port 7860
REM    stop-harvestr.bat 8080       -> stops the instance on that port
REM ===========================================================================
setlocal EnableExtensions

set "PORT=%~1"
if "%PORT%"=="" set "PORT=7860"

set "PID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    if not defined PID set "PID=%%P"
)

if not defined PID (
    echo Nothing is listening on port %PORT% - Harvestr is not running.
    exit /b 0
)

echo Stopping Harvestr (pid %PID%) on port %PORT%...

REM --- Drain first, THEN kill --------------------------------------------
REM This matters more than it looks. The recordings drive is exFAT, which has
REM no journal: hard-killing while 60-80 ffmpeg processes are mid-write can
REM leave directory entries that list fine but reject every create afterwards
REM ("corrupted and unreadable", WinError 1392) and need chkdsk to repair.
REM Four model folders were lost that way.
REM
REM So ask the app to stop all recording first and give ffmpeg a moment to
REM close its files cleanly. Best-effort: if the API doesn't answer we still
REM fall through to the kill below.
echo   asking recorders to stop cleanly...
powershell -NoProfile -Command ^
  "try { Invoke-RestMethod -Uri 'http://127.0.0.1:%PORT%/api/live/toggle_all' -Method Post -ContentType 'application/json' -Body '{\"running\":false}' -TimeoutSec 120 | Out-Null } catch { }" >nul 2>&1

REM Wait for ffmpeg to drain (up to ~30s), rather than a blind fixed sleep.
for /L %%i in (1,1,15) do (
    tasklist /FI "IMAGENAME eq ffmpeg.exe" 2>nul | find /I "ffmpeg.exe" >nul || goto :drained
    timeout /t 2 /nobreak >nul 2>&1
)
:drained

REM /T kills the process TREE, so any ffmpeg that didn't drain in time dies
REM with the parent instead of being orphaned holding its output file open.
REM /F is unavoidable: pythonw has no window, so the polite WM_CLOSE that
REM taskkill sends without /F is never received.
taskkill /PID %PID% /T /F >nul 2>&1
if errorlevel 1 (
    echo Could not stop pid %PID%. Try again from an elevated prompt:
    echo   taskkill /PID %PID% /T /F
    exit /b 1
)

echo Stopped.
exit /b 0
