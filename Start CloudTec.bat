@echo off
rem Double-click to start CloudTec Center and open it in your browser.
rem Keep the window open while you use it; closing the window stops CloudTec.
setlocal
title CloudTec Center
cd /d "%~dp0"

rem Already running? Just open the browser.
powershell -NoProfile -Command "try { Invoke-WebRequest http://127.0.0.1:8000/api/me -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 (
  echo CloudTec is already running. Opening it in your browser...
  start "" http://localhost:8000
  timeout /t 3 >nul
  exit /b 0
)

rem Find Python: the "py" launcher first, then "python".
set "PY="
py --version >nul 2>nul
if not errorlevel 1 (
  set "PY=py"
) else (
  python --version >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo Python is not installed.
  echo.
  echo Install it from https://www.python.org/downloads/
  echo On the first screen of the installer, tick "Add python.exe to PATH".
  echo Then double-click this file again.
  echo.
  pause
  exit /b 1
)

rem Install the components on first run.
%PY% -c "import fastapi, uvicorn, ldap3, httpx, cryptography" >nul 2>nul
if errorlevel 1 (
  echo Installing CloudTec components. This only happens the first time...
  echo.
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Installing failed. Send a screenshot of this window for help.
    pause
    exit /b 1
  )
)

echo.
echo   CloudTec Center is starting at http://localhost:8000
echo   Your browser will open in a few seconds.
echo.
echo   Keep this window open while you use CloudTec.
echo   Close this window to stop CloudTec.
echo.
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 4; Start-Process 'http://localhost:8000'"
%PY% -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000

echo.
echo CloudTec has stopped.
pause
