@echo off
REM ===========================================================================
REM  Start Harvestr detached, so it keeps recording after you close this
REM  window. Uses pythonw.exe (no console) and hands the process off via
REM  Start-Process, so it is NOT a child of this terminal.
REM
REM    start-harvestr.bat            -> http://127.0.0.1:7860
REM    start-harvestr.bat 8080       -> pick a different port
REM
REM  Stop it with stop-harvestr.bat.
REM ===========================================================================
setlocal EnableExtensions

set "PORT=%~1"
if "%PORT%"=="" set "PORT=7860"

cd /d "%~dp0"

REM --- Refuse to start a second copy -----------------------------------------
REM Two instances would both restore the whole tracked fleet, write the same
REM live_models.json and record into the same folders - which corrupts state
REM and doubles the bandwidth. The listening port is the cheapest reliable
REM "already running" check.
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo Harvestr is already running on port %PORT%.
    echo   http://127.0.0.1:%PORT%
    exit /b 0
)

REM --- Locate pythonw.exe ----------------------------------------------------
REM pythonw is python without a console window. Falling back to python.exe
REM still works, it just leaves a console around.
set "PYW="
for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do (
    if not defined PYW set "PYW=%%P"
)
if not defined PYW (
    for /f "delims=" %%P in ('where python.exe 2^>nul') do (
        if not defined PYW set "PYW=%%~dpPpythonw.exe"
    )
)
if not defined PYW goto :nopython
if not exist "%PYW%" (
    for /f "delims=" %%P in ('where python.exe 2^>nul') do (
        set "PYW=%%P"
        goto :gotpython
    )
    goto :nopython
)
:gotpython

if not exist "logs" mkdir "logs"

REM --- Launch detached -------------------------------------------------------
REM Start-Process gives a real detached process plus stdout/stderr capture.
REM The app already writes logs\live-errors.log itself; these two files exist
REM to catch a STARTUP traceback, which would otherwise vanish with no console.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process -FilePath '%PYW%' -ArgumentList 'webui.py','--host','127.0.0.1','--port','%PORT%' -WorkingDirectory '%CD%' -RedirectStandardOutput 'logs\webui.out.log' -RedirectStandardError 'logs\webui.err.log' -WindowStyle Hidden"
if errorlevel 1 goto :launchfail

echo Harvestr starting on port %PORT%.
echo.
echo   URL   : http://127.0.0.1:%PORT%
echo   Logs  : logs\webui.out.log  /  logs\webui.err.log
echo   Stop  : stop-harvestr.bat
echo.
echo The web UI answers immediately; restoring the tracked models takes a few
echo minutes, and recording starts on its own as models come online.
echo Safe to close this window.
exit /b 0

:nopython
echo ERROR: could not find python on PATH.
echo Install Python 3, or run manually:  python webui.py --port %PORT%
exit /b 1

:launchfail
echo ERROR: failed to launch. Try running manually to see the error:
echo   python webui.py --port %PORT%
exit /b 1
