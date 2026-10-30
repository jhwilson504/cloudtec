@echo off
rem Double-click to start KasTec Center and open it in your browser.
rem Keep the window open while you use it; closing the window stops KasTec.
rem After "Set up network access" it serves HTTPS on the network instead of http://localhost:8000.
setlocal
title KasTec Center
cd /d "%~dp0"
rem The launcher used to be called "Start CloudTec.bat": remove it so there is only one.
if exist "Start CloudTec.bat" del "Start CloudTec.bat"

rem Already running? Stop it (only if it really is KasTec), so this always starts the version in this folder.
powershell -NoProfile -Command "$ports = @(8000); try { $n = Get-Content 'data\network.json' -Raw -ErrorAction Stop | ConvertFrom-Json; if ($n.port) { $ports += [int]$n.port } } catch {}; $ids = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $ports -contains $_.LocalPort } | Select-Object -ExpandProperty OwningProcess -Unique; foreach ($i in $ids) { $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $i); if ($p.CommandLine -match 'app\.(run|main)') { Write-Host 'KasTec is already running. Restarting it so you get the latest version...'; Stop-Process -Id $i -Force; Start-Sleep 2 } }"

call :find_python || exit /b 1

%PY% -m app.run --open-browser

echo.
echo KasTec has stopped.
pause
exit /b 0

:find_python
rem Find Python: the "py" launcher first, then "python". Install KasTec's components on first run.
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
%PY% -c "import fastapi, uvicorn, ldap3, httpx, cryptography" >nul 2>nul
if errorlevel 1 (
  echo Installing KasTec components. This only happens the first time...
  echo.
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Installing failed. Send a screenshot of this window for help.
    pause
    exit /b 1
  )
)
exit /b 0
