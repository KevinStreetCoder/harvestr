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

REM /T kills the process TREE. The recorder spawns one ffmpeg per active
REM capture; without /T those would be orphaned, keep running, and hold their
REM output files open.
REM
REM /F is unavoidable: pythonw has no window, so the polite WM_CLOSE that
REM taskkill sends without /F is never received. In-flight captures therefore
REM end abruptly - the partial .tmp.ts files they leave are still complete,
REM playable recordings up to that moment, they just stop early.
taskkill /PID %PID% /T /F >nul 2>&1
if errorlevel 1 (
    echo Could not stop pid %PID%. Try again from an elevated prompt:
    echo   taskkill /PID %PID% /T /F
    exit /b 1
)

echo Stopped.
exit /b 0
